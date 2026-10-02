# -*- coding: utf-8 -*-
"""
跨语言契约回归：**帧的身份是 (stream, frame_id)，不是 frame_id**。

背景
----
发送端（Python）的视频与音频各有**独立的** frame_id 计数器，都从 1 开始递增：

    video: 1, 2, 3, ...
    audio: 1, 2, 3, ...

投影仪端（Java `FrameReassembler`）早期只用 frame_id 当重组表的 key，
于是两条流在同一个编号上会共用一份残帧状态：
谁先到谁建状态，后到的那一路因为 `frag_count` 对不上被直接丢弃。

什么情况下会真的撞上：**只要一帧的分片不是连续到达**。UDP 没有顺序保证，
乱序/重传重排都会造成交织；发送端若为降延迟把音频插在视频分片之间也一样。
TCP 单流顺序发送时两条流的分片各自连续，多数时候侥幸不冲突 ——
所以这个 bug 表现得很"间歇"，最难查。

这个测试把 Java 端的重组逻辑（新旧两种 key 方式）各复刻一份，
用真正的 `castlink.protocol.pack_frame` 造交织数据喂进去对比。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from castlink import protocol as P  # noqa: E402

FRAMES = 30
FRAG = 512
AUDIO_BYTES = 300        # ≤ FRAG，音频帧一个分片就够


class JavaReassembler:
    """复刻 receiver/android/.../FrameReassembler.java 的重组逻辑。"""

    def __init__(self, composite_key: bool):
        self.composite_key = composite_key
        self.partials = {}
        self.assembled = []

    def _key(self, stream: int, frame_id: int):
        # 旧实现：只用帧号 —— 两条流共用同一格
        return (stream, frame_id) if self.composite_key else frame_id

    def feed(self, pkt: bytes):
        h = P.FrameHeader.unpack(pkt)
        payload = pkt[P.HEADER_SIZE:P.HEADER_SIZE + h.payload_len]
        k = self._key(h.stream, h.frame_id)

        p = self.partials.get(k)
        if p is None:
            p = {"stream": h.stream, "count": h.frag_count,
                 "parts": [None] * h.frag_count}
            self.partials[k] = p
        # 关键：编号撞车时，后到的那一路 frag_count 对不上，整帧被丢
        if p["count"] != h.frag_count or p["parts"][h.frag_idx] is not None:
            return
        p["parts"][h.frag_idx] = payload

        if all(x is not None for x in p["parts"]):
            self.partials.pop(self._key(p["stream"], h.frame_id), None)
            self.assembled.append((p["stream"], h.frame_id))


def build_packets():
    """
    造一条**交织**的报文序列：视频帧的分片中间插入同号的音频帧。

    这正是 UDP 乱序（或发送端交织发送音视频）时的真实到达顺序。
    """
    order = []
    for i in range(1, FRAMES + 1):
        v = b"\x00\x00\x00\x01\x26" + bytes(4 * FRAG)      # 5 个分片
        vpkts = P.pack_frame(P.STREAM_VIDEO, P.CODEC_H265, i, i * 1000, v, False, FRAG)
        a = b"\xff\xf1" + bytes(AUDIO_BYTES)               # 1 个分片
        apkts = P.pack_frame(P.STREAM_AUDIO, P.CODEC_AAC, i, i * 1000, a, False, FRAG)
        order.append(vpkts[0])              # 视频首片 —— 建立视频残帧
        order.extend(apkts)                 # 同号音频帧插进来（撞车点）
        order.extend(vpkts[1:])             # 视频剩余分片
    return order


def main() -> int:
    packets = build_packets()
    print(f"构造报文 {len(packets)} 个（{FRAMES} 帧视频 + {FRAMES} 帧音频，交织到达）")

    stats = {}
    for label, composite in (("旧实现（只按 frame_id）", False),
                             ("新实现（按 stream+frame_id）", True)):
        r = JavaReassembler(composite_key=composite)
        for pkt in packets:
            r.feed(pkt)
        v = sum(1 for s, _ in r.assembled if s == P.STREAM_VIDEO)
        a = sum(1 for s, _ in r.assembled if s == P.STREAM_AUDIO)
        stats[label] = (v, a)
        print(f"  {label}: 视频 {v}/{FRAMES}，音频 {a}/{FRAMES}")

    old_v, old_a = stats["旧实现（只按 frame_id）"]
    new_v, new_a = stats["新实现（按 stream+frame_id）"]

    print()
    if old_v + old_a >= FRAMES * 2:
        print("⚠ 旧实现居然一帧没丢 —— 说明测试没能制造出帧号撞车，测试本身失效。")
        return 2
    if (new_v, new_a) != (FRAMES, FRAMES):
        print(f"✗ 新实现仍有丢失（视频 {new_v}/{FRAMES}，音频 {new_a}/{FRAMES}）")
        return 1

    lost = "音频" if old_a < FRAMES else "视频"
    print(f"✓ 结论：旧实现因为帧号撞车丢掉 {lost}帧；"
          f"新实现 {FRAMES} 视频 + {FRAMES} 音频全收。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
