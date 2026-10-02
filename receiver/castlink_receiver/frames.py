# -*- coding: utf-8 -*-
"""
UDP 的分片是不保证顺序、也不保证可达的。这个重组器只做一件事：

    **每到一个分片就记录，攒齐即成帧；攒不齐就超时丢弃，绝不半帧外传。**

为什么坚决丢半帧 —— 丢半帧只损失一帧，
而把缺了 NAL 的数据喂给解码器，轻则花屏、重则解码器进入错误状态，
后面几十帧都跟着花。宁丢勿错。
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from castlink.protocol import (FrameHeader, HEADER_SIZE, MAX_PAYLOAD,
                               STREAM_VIDEO, STREAM_AUDIO, F_KEYFRAME)

# 超过此上限就认为该帧已经无望收齐（UDP 抖动保护）
PARTIAL_TTL_MS = 250
# 内存中最多缓存多少未成帧的数据（字节），防止弱网下无限堆积
MAX_BUFFER_BYTES = 48 * 1024 * 1024


class FrameAssembler:
    """
    feed_packet / feed_bytes -> 回调 (stream, codec, pts, payload, keyframe)
    """

    def __init__(self,
                 on_frame: Callable[[int, int, int, bytes, bool], None],
                 on_drop: Callable[[int, int], None] = lambda stream, fid: None,
                 log=print):
        self.on_frame = on_frame
        self.on_drop = on_drop
        self.log = log
        self._partial: Dict[int, dict] = {}
        self._buffered = 0
        self._pending = bytearray()      # TCP 场景下未构成完整报文的残余字节
        self._max_seen = -1

        self.frames_assembled = 0
        self.frames_dropped = 0
        self.packets_lost = 0
        self.last_keyframe_at = 0.0
        self._resync_logged = 0

    def _log_resync(self, msg: str):
        """重同步类日志只打前几条 —— 弱网下刷屏会淹没真正有用的信息。"""
        self._resync_logged += 1
        if self._resync_logged <= 5:
            self.log(f"报文解析异常: {msg}")

    # -------------------------------------------------------------- 入口
    def feed_bytes(self, chunk: bytes):
        """TCP：字节流 -> 逐个 CAST 报文。"""
        self._pending.extend(chunk)
        buf = self._pending
        consumed = 0
        while True:
            rest = len(buf) - consumed
            if rest < HEADER_SIZE:
                break
            # 头部解析统一交给协议模块，避免手工算字节偏移出错。
            # 注意 require_payload=False：这里只是"窥头"，净荷没到齐是常态，
            # 不能当成错误去触发重同步，否则整条字节流会被逐字节滑坏。
            try:
                hdr = FrameHeader.unpack(
                    bytes(buf[consumed:consumed + HEADER_SIZE]), require_payload=False)
            except ValueError as e:
                self._log_resync(str(e))
                consumed += 1          # 流式字节流偶发错位时向前滑一个字节重新同步
                continue
            # 合理性兜底：空净荷或超过协议上限的长度说明已经失步，不能傻等
            if hdr.payload_len == 0 or hdr.payload_len > MAX_PAYLOAD:
                self._log_resync(f"净荷长度异常: {hdr.payload_len}")
                consumed += 1
                continue
            total = HEADER_SIZE + hdr.payload_len
            if rest < total:
                break
            pkt = bytes(buf[consumed:consumed + total])
            consumed += total
            try:
                self.feed_packet(pkt)
            except Exception as e:
                self._log_resync(str(e))
        if consumed:
            del self._pending[:consumed]

    def feed_packet(self, pkt: bytes):
        hdr = FrameHeader.unpack(pkt)
        payload = pkt[HEADER_SIZE:HEADER_SIZE + hdr.payload_len]
        now_ms = time.time() * 1000.0

        slot = self._partial.get(hdr.frame_id)
        if slot is None:
            slot = {"stream": hdr.stream, "codec": hdr.codec,
                    "count": hdr.frag_count, "size": hdr.total_size,
                    "pts": hdr.pts, "parts": {}, "recv": 0,
                    "created": now_ms,
                    "key": bool(hdr.flags & F_KEYFRAME)}
            self._partial[hdr.frame_id] = slot

        if slot["count"] != hdr.frag_count:
            # 同一 frame_id 被异常复用，直接重置
            self._drop(hdr.frame_id, True)
            return
        if hdr.frag_idx in slot["parts"]:
            return
        slot["parts"][hdr.frag_idx] = payload
        slot["recv"] += len(payload)
        self._buffered += len(payload)

        if len(slot["parts"]) == slot["count"]:
            self._emit(hdr.frame_id)
        self._sweep(now_ms)

    # -------------------------------------------------------------- 内部
    def _emit(self, frame_id: int):
        slot = self._partial.pop(frame_id, None)
        if not slot:
            return
        data = b"".join(slot["parts"][i] for i in range(slot["count"]))
        self._buffered -= slot["recv"]
        self.frames_assembled += 1
        if slot["key"]:
            self.last_keyframe_at = time.time()
        if frame_id > self._max_seen:
            self._max_seen = frame_id
        self.on_frame(slot["stream"], slot["codec"], slot["pts"], data, slot["key"])

    def _drop(self, frame_id: int, silent: bool = False):
        slot = self._partial.pop(frame_id, None)
        if not slot:
            return
        self._buffered -= slot["recv"]
        self.frames_dropped += 1
        self.packets_lost += slot["count"] - len(slot["parts"])
        if not silent:
            self.on_drop(slot["stream"], frame_id)

    def _sweep(self, now_ms: float):
        """清理超时未收齐的帧；并对缓冲区做上限保护。"""
        for fid in list(self._partial.keys()):
            slot = self._partial[fid]
            if now_ms - slot["created"] > PARTIAL_TTL_MS or fid < self._max_seen - 120:
                self._drop(fid)
        if self._buffered > MAX_BUFFER_BYTES:
            ordered = sorted(self._partial.keys())
            for fid in ordered[:max(1, len(ordered) // 3)]:
                self._drop(fid)

    # -------------------------------------------------------------- 统计
    def reset(self):
        self._partial.clear()
        self._buffered = 0
        del self._pending[:]
        self._max_seen = -1
        self.frames_assembled = 0
        self.frames_dropped = 0
        self.packets_lost = 0

    def flush(self):
        """尽力把还差一点没凑齐的帧丢掉（会话结束时调用）。"""
        for fid in list(self._partial.keys()):
            self._drop(fid, silent=True)

    def snapshot(self) -> dict:
        return {
            "assembled": self.frames_assembled,
            "dropped": self.frames_dropped,
            "lost_packets": self.packets_lost,
            "partial": len(self._partial),
            "buffered_bytes": self._buffered,
        }
