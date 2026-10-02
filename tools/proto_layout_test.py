# -*- coding: utf-8 -*-
"""
CAST/1 头部布局的跨语言对拍测试。

背景
----
发送端（Python）和 Windows 接收端共用 `castlink/protocol.py`，
彼此一致，所以 Python<->Python 的联调测试永远发现不了头部布局问题。
但投影仪端（Android）是**独立实现**——`Frames.java` / `FrameReassembler.java`，
一旦两边的字节布局不一致，接收端会静默丢弃所有报文（表现：黑屏 + 无声），
而发送端界面上一切正常。

这个测试做三件事：
  1. 断言 Python 头部布局 == Java 头部布局（逐字段偏移 + 总长度）；
  2. 用 Python 逐行复刻 `FrameReassembler.java` 的解析逻辑，
     拿**真正的发送端数据**喂进去，检查能否重组出帧；
  3. 校验 TCP 字节流（含粘包/拆包）与 UDP 乱序两种路径。

运行：tools/pyenv/Scripts/python.exe tools/proto_layout_test.py
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from castlink.protocol import (  # noqa: E402
    HEADER_STRUCT, HEADER_SIZE, FrameHeader, pack_frame,
    STREAM_VIDEO, STREAM_AUDIO, CODEC_H265, CODEC_AAC,
    MAX_TCP_PAYLOAD, MAX_UDP_PAYLOAD,
)

# --------------------------------------------------------------------------
# Java 侧的期望值 —— 逐字抄自 receiver/android/java/com/castlink/receiver/
#   Frames.java            : HEADER_SIZE = 32
#   FrameReassembler.java  : 读取各字段时用的偏移
# 这些数字是"契约"，改动需要两端同时改。
# --------------------------------------------------------------------------
JAVA_HEADER_SIZE = 32
JAVA_OFFSETS = {
    "magic": 0,
    "version": 4,
    "stream": 5,
    "codec": 6,
    "flags": 7,
    "frame_id": 8,        # FrameReassembler: u32(pkt, off + 8)
    "frag_idx": 12,       # u16(pkt, off + 12)
    "frag_count": 14,     # u16(pkt, off + 14)
    "total_size": 16,     # u32(pkt, off + 16)
    "payload_len": 20,    # u16(pkt, off + 20)
    "pts": 24,            # u64(pkt, off + 24)
}


# 头部字段的**顺序**契约（不含可选填充字节）。
# 若有人往结构体里增删一个单字节字段，下面的断言会立刻报"字段数不符"，
# 而不是让偏移静默错位。
FIELDS_IN_ORDER = ["magic", "version", "stream", "codec", "flags",
                   "frame_id", "frag_idx", "frag_count", "total_size",
                   "payload_len", "pad2", "pts"]

_CHAR_SIZE = {"s": 1, "B": 1, "b": 1, "H": 2, "h": 2, "I": 4, "i": 4, "Q": 8, "q": 8}


def python_offsets() -> dict:
    """解析 HEADER_STRUCT 的真实格式串，算出每个字段的字节偏移。"""
    import re
    fmt = HEADER_STRUCT.format
    assert fmt.startswith("<"), f"预期小端无对齐格式，实际 {fmt!r}"
    body = fmt[1:]
    tokens = re.findall(r"(\d*)([sBbHhIiQq])", body)
    assert "".join(n + c for n, c in tokens) == body, f"无法解析格式串 {fmt!r}"

    # 展开成"一个字段一个元素"：
    #   '4s' 是一个 4 字节字段（magic），不是 4 个字段；
    #   'BBBB' 是 4 个各自 1 字节的字段。
    expanded = []
    for count, char in tokens:
        n = int(count) if count else 1
        if char == "s":
            expanded.append(n)
        else:
            expanded.extend([_CHAR_SIZE[char]] * n)

    assert len(expanded) == len(FIELDS_IN_ORDER), (
        f"头部字段数与契约不符：格式串展开 {len(expanded)} 个字段，"
        f"契约 {len(FIELDS_IN_ORDER)} 个 —— protocol.py 的单字节字段数量被改过了？")

    out, off = {}, 0
    for name, size in zip(FIELDS_IN_ORDER, expanded):
        out[name] = off
        off += size
    return out


# ==========================================================================
# FrameReassembler.java 的 Python 复刻（逐行对照，不"改进"）
# ==========================================================================
class JavaReassembler:
    """忠实复刻 Android 端的重组器，用于验证发送端数据能否被它解析。"""

    HEADER_SIZE = JAVA_HEADER_SIZE  # Frames.HEADER_SIZE

    def __init__(self):
        self.partials = {}
        self.leftover = b""
        self.assembled = 0
        self.dropped = 0
        self.lost_packets = 0
        self.rejected_magic = 0
        self.rejected_version = 0
        self.rejected_frag = 0
        self.rejected_short = 0
        # 诊断用：记录第一个报文被解析出来的关键字段
        self.first_seen = None

    # -- Frames.java 的三个小端读取器 --
    @staticmethod
    def u16(b, off):
        return b[off] | (b[off + 1] << 8)

    @staticmethod
    def u32(b, off):
        return b[off] | (b[off + 1] << 8) | (b[off + 2] << 16) | (b[off + 3] << 24)

    @staticmethod
    def u64(b, off):
        v = 0
        for i in range(7, -1, -1):
            v = ((v << 8) | b[off + i]) & 0xFFFFFFFFFFFFFFFF
        return v

    # -- feedBytes --
    def feed_bytes(self, buf: bytes):
        work = self.leftover + buf
        off = 0
        end = len(work)
        while True:
            remain = end - off
            if remain < self.HEADER_SIZE:
                break
            payload_len = self.u16(work, off + 20)
            total = self.HEADER_SIZE + payload_len
            if remain < total:
                break
            self.feed_packet(work[off:off + total])
            off += total
        self.leftover = work[off:]

    # -- feedPacket --
    def feed_packet(self, pkt: bytes):
        if len(pkt) < self.HEADER_SIZE:
            self.rejected_short += 1
            return
        if pkt[0:4] != b"CAST":
            self.rejected_magic += 1
            return
        if pkt[4] != 1:
            self.rejected_version += 1
            return
        stream = pkt[5]
        codec = pkt[6]
        flags = pkt[7]
        frame_id = self.u32(pkt, 8)
        frag_idx = self.u16(pkt, 12)
        frag_count = self.u16(pkt, 14)
        total_size = self.u32(pkt, 16)
        payload_len = self.u16(pkt, 20)
        pts = self.u64(pkt, 24)
        if self.first_seen is None:
            self.first_seen = {
                "stream": stream, "codec": codec, "frame_id": frame_id,
                "frag_idx": frag_idx, "frag_count": frag_count,
                "total_size": total_size, "payload_len": payload_len, "pts": pts,
                "pkt_len": len(pkt),
            }
        if len(pkt) < self.HEADER_SIZE + payload_len:
            self.rejected_short += 1
            return
        if frag_count <= 0 or frag_idx >= frag_count:
            self.rejected_frag += 1
            return

        p = self.partials.get(frame_id)
        if p is None:
            p = {"frame_id": frame_id, "stream": stream, "count": frag_count,
                 "size": total_size, "pts": pts, "key": bool(flags & 1),
                 "parts": [None] * frag_count}
            self.partials[frame_id] = p
        if p["count"] != frag_count or p["parts"][frag_idx] is not None:
            return
        p["parts"][frag_idx] = pkt[self.HEADER_SIZE:self.HEADER_SIZE + payload_len]

        if all(x is not None for x in p["parts"]):
            self._emit(p)

    def _emit(self, p):
        self.partials.pop(p["frame_id"], None)
        self.assembled += 1

    def pending(self):
        """未收齐的残帧数量（Java 端 250ms 后会被 sweep 丢弃）。"""
        return len(self.partials)


# ==========================================================================
def make_frames(n=6, payload_size=200_000):
    """造一批形状接近真实视频的帧：首帧关键帧、大帧会被分很多片。"""
    frames = []
    for i in range(n):
        size = payload_size if i % 3 else payload_size // 8
        payload = bytes((i * 7 + j) & 0xFF for j in range(size))
        frames.append({"stream": STREAM_VIDEO, "codec": CODEC_H265,
                       "frame_id": i + 1, "pts": i * 33_333,
                       "payload": payload, "keyframe": (i == 0)})
    return frames


def run_case(title: str, frag_size: int, chunked: bool) -> bool:
    frames = make_frames()
    reassembler = JavaReassembler()
    total_bytes = 0

    stream = b""
    for f in frames:
        for pkt in pack_frame(f["stream"], f["codec"], f["frame_id"],
                              f["pts"], f["payload"], f["keyframe"], frag_size):
            stream += pkt
    total_bytes = len(stream)

    if chunked:
        # 模拟 TCP 粘包/拆包：用奇怪的分块大小喂进去
        pos = 0
        for step in (1234, 20000, 7, 65536, 333, 100000):
            if pos >= len(stream):
                break
            reassembler.feed_bytes(stream[pos:pos + step])
            pos += step
        while pos < len(stream):
            reassembler.feed_bytes(stream[pos:pos + 40000])
            pos += 40000
    else:
        # UDP：逐个报文（并打乱顺序，验证乱序容忍）
        import random
        pkts = []
        for f in frames:
            pkts.extend(pack_frame(f["stream"], f["codec"], f["frame_id"],
                                   f["pts"], f["payload"], f["keyframe"], frag_size))
        random.Random(1234).shuffle(pkts)
        for pkt in pkts:
            reassembler.feed_packet(pkt)

    ok = reassembler.assembled == len(frames)
    print(f"  {title}")
    print(f"    发送 {len(frames)} 帧 / {total_bytes} 字节")
    print(f"    Java 端重组成功: {reassembler.assembled} 帧  "
          f"残帧 {reassembler.pending()}  半帧丢弃 {reassembler.rejected_short}")
    if reassembler.first_seen is not None:
        fs = reassembler.first_seen
        print(f"    Java 端从首报文解析出: payload_len={fs['payload_len']} "
              f"(实际 {min(frag_size, len(frames[0]['payload']))}), "
              f"frag_count={fs['frag_count']}, frame_id={fs['frame_id']}")
    print(f"    {'✔ 通过' if ok else '✘ 失败 —— 接收端无法重组任何一帧'}")
    return ok


def main() -> int:
    print("=" * 74)
    print("1) 头部布局契约：Python 实现 vs Java 实现")
    print("=" * 74)
    py_off = python_offsets()
    print(f"  Python HEADER_SIZE = {HEADER_SIZE}")
    print(f"  Java   HEADER_SIZE = {JAVA_HEADER_SIZE}")
    mismatch = []
    if HEADER_SIZE != JAVA_HEADER_SIZE:
        mismatch.append(f"HEADER_SIZE: Python={HEADER_SIZE} Java={JAVA_HEADER_SIZE}")
    for name, java_off in JAVA_OFFSETS.items():
        py = py_off.get(name)
        flag = "  " if py == java_off else "✘ "
        if py != java_off:
            mismatch.append(f"{name}: Python={py} Java={java_off}")
        print(f"  {flag}{name:12s} Python={py:>3}  Java={java_off:>3}")

    print()
    print("=" * 74)
    print("2) 用真正的发送端数据喂给 Java 端重组逻辑")
    print("=" * 74)
    ok_tcp = run_case("TCP 字节流（含粘包/超长分片）", MAX_TCP_PAYLOAD, chunked=True)
    print()
    ok_udp = run_case("UDP 报文（乱序到达）", MAX_UDP_PAYLOAD, chunked=False)

    print()
    print("=" * 74)
    if mismatch:
        print("结论：头部布局不一致 —— 接收端会丢弃所有报文（黑屏 + 无声）")
        for m in mismatch:
            print("   - " + m)
    if mismatch or not (ok_tcp and ok_udp):
        print("结果：✘ 失败")
        return 1
    print("结论：两端布局一致，接收端可正常重组")
    print("结果：✔ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
