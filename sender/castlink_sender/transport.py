# -*- coding: utf-8 -*-
"""
媒体传输层（发送端）。

两条通道，共用同一套 CAST 帧格式：

* **可靠模式（TCP）** —— 默认选项。TCP 重传保证在途数据不丢，代价是弱网下
  容易积压，因此配合"严格有界队列 + 丢到下一个关键帧"的实时策略
  （见 MediaSender 的类注释：**不做背压**，背压会把整条链路拖死）。
* **低延迟模式（UDP）** —— 每个报文 ≤1184 字节，绝不分片（避免 UDP 分片放大丢包）。
  接收端校验分片完整性，缺片即整帧丢弃，并向发送端请求新的关键帧。

发送线程与采集线程解耦：采集线程只管往队列里排 Frame 任务，
真正的 socket 写操作在独立线程完成，避免 encode 被网络抖动拖慢。
**音频单独排队并优先发送** —— 视频积压时音频绝不能被堵在后面。
"""
from __future__ import annotations

import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from castlink.protocol import (
    FrameHeader, MAX_TCP_PAYLOAD, MAX_UDP_PAYLOAD, pack_frame, STREAM_VIDEO, STREAM_AUDIO,
)

MODE_TCP = "tcp"
MODE_UDP = "udp"


@dataclass
class FrameTask:
    stream: int
    codec: int
    frame_id: int
    pts: int
    payload: bytes
    keyframe: bool = False
    # 这一帧从"编码器吐出来"到现在已经等了多久（微秒）。
    # 传输层用它做**延迟上限**：等太久的非关键帧已经没有实时价值，直接丢。
    lag_us: int = 0


@dataclass
class TxStats:
    bytes_total: int = 0
    frames_total: int = 0
    dropped: int = 0
    dropped_video: int = 0
    dropped_audio: int = 0
    dropped_late: int = 0
    queued: int = 0
    bitrate_mbps: float = 0.0
    fps: float = 0.0

    def sample(self) -> dict:
        return {
            "bytes": self.bytes_total, "frames": self.frames_total,
            "dropped": self.dropped,
            "dropped_video": self.dropped_video,
            "dropped_audio": self.dropped_audio,
            "dropped_late": self.dropped_late,
            "queued": self.queued,
            "bitrate": round(self.bitrate_mbps, 2), "fps": round(self.fps, 2),
        }


class MediaSender:
    """
    把 FrameTask 排队并写到网络。

    <p><b>为什么"实时"必须靠丢帧，而不是靠背压</b>

    早期 TCP 模式下的策略是"宁愿背压也不丢数据"：队列满了就让采集线程 sleep，
    socket 写阻塞直接传导回 ffmpeg 的 stdout 管道。静态画面看不出问题，
    一旦接收端解码跟不上（或链路带宽不够），就会形成一条死锁式的慢速通路：

        接收端解码慢 → 它的 TCP 收缓冲填满 → 窗口关闭 → sendall 阻塞
        → 队列填满 → 采集线程停住 → ffmpeg stdout 不再被读走 → 编码器停摆

    表现就是**画面停在某一帧再也不动**（用户看到的是"只有静态画面、不能实时同步"），
    而且因为队列里压着上百帧，恢复时又是一大段"回放"。

    现在改成真正的实时语义：队列严格有界（4 帧 ≈ 0.17 秒 @24fps），
    一旦溢出就**丢弃到下一个关键帧**——先丢掉已经过时的 P 帧，
    再在关键帧到来时整体清空队列、从关键帧重新开始。
    这样延迟恒定，代价只是掉几帧，而不会整条链路冻死。
    同时通过 on_congestion 回调通知会话层重新出 IDR 并降档。

    <p>v1.0.6 又加了一道 **延迟上限**（LATENCY_CAP_US）：光靠"队列有界"
    还不够 —— 线程写 socket 时同样会阻塞（TCP 窗口关闭），那段时间不在队列
    里却照样在累积延迟。所以每帧都带上"从编码器出来到现在等了多久"，
    超过 250ms 的非关键帧一律不要。这样无论链路多慢，画面滞后都被钉在
    一个固定的上限内，而不是随缓冲慢慢漂。

    音频单独一条队列并优先发送：音频码率低（~200 kbps），
    绝不会饿死视频；但反过来，视频积压时音频**不能**被堵在后面 ——
    否则声音会比画面晚好几秒，听起来就是"没有声音"。
    """

    # 视频队列上限：按帧数算。
    #
    # <p><b>v1.0.6：从 8 / 24 砍到 4 / 6 —— 用户报的"画面有 1 秒延迟"就在这里。</b>
    #
    # 队列深度**直接就是端到端延迟**：帧一进队列就在等网络，等完才发出去。
    # 旧值 24 帧是"给无损模式留余量"想出来的，但 24 帧 @24fps 恰好 = 1.0 秒，
    # 和用户看到的延迟数字分毫不差。TCP 在局域网里几乎不会因为缓冲小而丢数据，
    # 小队列只会在真正拥塞时丢掉已经过时的帧 —— 那正是我们想要的。
    #
    # 现在：low-latency 4 帧（@24fps ≈ 0.17 秒）、可靠模式 6 帧（≈ 0.25 秒）。
    QUEUE_MAX_VIDEO = 4
    # 可靠模式（用户显式选了"无损/可靠"）稍放宽，但仍在 0.3 秒量级内。
    QUEUE_MAX_VIDEO_RELIABLE = 6
    # 音频队列上限：AAC 每帧 21ms。
    # 旧值 64 帧 ≈ 1.4 秒 —— 声音比画面晚一秒多，听起来就是"声音有延迟"。
    # 16 帧 ≈ 0.34 秒，配合接收端的小播放缓冲足够平滑。
    QUEUE_MAX_AUDIO = 16

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self.mode = MODE_TCP
        self.sock: Optional[socket.socket] = None
        self.udp_addr: Optional[Tuple[str, int]] = None
        self._qv: "queue.Queue[FrameTask]" = queue.Queue()
        self._qa: "queue.Queue[FrameTask]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # 每次 start() 递增：发送线程带着自己那一代编号跑，
        # stop() 之后残留的老线程看到代号变了就自己收摊，
        # 绝不会把上一轮的帧写进新一轮的 socket。
        self._generation = 0
        self.stats = TxStats()
        self._lock = threading.Lock()
        self._win_start = time.perf_counter()
        self._win_bytes = 0
        self._win_frames = 0
        # 丢帧状态机：一旦因为拥塞丢了非关键帧，就一路丢到下一个关键帧，
        # 否则解码链会因为缺参考帧而花屏/报错。
        self._drop_video_until_key = False
        # 拥塞回调：由会话层接上，用来"重新出 IDR + 降档"
        self.on_congestion: Optional[Callable[[str], None]] = None
        self._last_congestion_ns = 0
        # 本次会话的目标码率（Mbps）—— 用它算发送缓冲的字节数。
        self._bitrate_mbps = 0.0

    def set_bitrate(self, mbps: float):
        """告诉传输层本次的码率，用来把发送缓冲换算成"固定秒数"。"""
        try:
            self._bitrate_mbps = max(0.0, float(mbps or 0))
        except Exception:
            self._bitrate_mbps = 0.0

    def sndbuf_bytes(self) -> int:
        """按时间预算算发送缓冲大小；未知码率时用下限。"""
        if self._bitrate_mbps <= 0:
            return self.SO_SNDBUF_MIN
        n = int(self._bitrate_mbps * 1_000_000 / 8 * self.BUFFER_SECONDS)
        return max(self.SO_SNDBUF_MIN, min(self.SO_SNDBUF_MAX, n))

    # --------- 生命周期 ---------
    # 发送缓冲刻意**开小**（默认通常 64KB，我们给 512KB）。
    #
    # 这里吃过一次大亏：早期设成 4MB。socket 缓冲是"发出去但还没被处理"的数据，
    # 它直接等于**端到端延迟**：接收端解码慢时，内核会先吃满这 4MB，
    # 再叠加接收端的 8MB 收缓冲 —— 一共 12MB 的在途数据。
    # 按 1080p/24fps/均衡档约 4 Mbps 算，12MB ≈ 24 秒延迟。
    # 用户的原话是"不能实时同步"：画面确实在动，但比电脑慢十几秒，
    # 而发送端一切正常、队列也是空的，只看统计根本发现不了。
    #
    # v1.0.6 再砍一半多：512KB 在 1080p/24fps/流畅档（约 4 Mbps）下等于
    # **1 秒** 的在途数据，正好又和用户看到的延迟对上。
    #
    # <p><b>但"写死一个字节数"本身就是错的：</b>同一个 512KB，
    # 在 4 Mbps 是 1 秒延迟，在 100 Mbps 只有 40ms。延迟是**时间**，
    # 不是字节。所以现在改成按"时间预算"算：发送缓冲 = 目标码率 × 0.25 秒，
    # 并夹在 64KB~2MB 之间。这样无论用户选流畅档还是 4K 视觉无损，
    # 在途数据的时长都是同一个量级 —— 缓冲既够吃链路抖动（带宽时延积），
    # 又不会变成一段慢慢播放的"回放"。
    BUFFER_SECONDS = 0.25
    SO_SNDBUF_MIN = 64 * 1024
    SO_SNDBUF_MAX = 2 * 1024 * 1024

    # 延迟上限（微秒）：非关键帧从编码器出来到准备入队已经等了这么久，
    # 就说明链路积压了 —— 直接丢掉并进入"丢到下一个关键帧"状态。
    #
    # 这是队列有界之外的第二道保险：队列只约束"还没发的帧数"，
    # 而写 socket 本身也会阻塞（TCP 窗口关闭时），那部分等待不在队列里。
    # 用帧自己的滞留时间做判据，才能把**整条流水线**的滞后一起兜住。
    LATENCY_CAP_US = 250_000

    def attach_tcp(self, sock: socket.socket):
        self._drop_socket()
        self.mode = MODE_TCP
        self.sock = sock
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # 小缓冲还有个副作用是好的：写阻塞会**更快**传导回来，
            # 于是队列更快积压 → 拥塞更快被上报 → 降档更及时。
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.sndbuf_bytes())
        except Exception:
            pass

    def attach_udp(self, addr: Tuple[str, int], local_port: int = 0):
        self._drop_socket()
        self.mode = MODE_UDP
        self.udp_addr = addr
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.sndbuf_bytes())
            self.sock.bind(("", local_port))
        except Exception as e:
            self.log(f"UDP 绑定失败: {e}")

    def _drop_socket(self):
        """换 socket 前先把旧的放掉，否则每投屏一次就漏一个 fd。"""
        old = self.sock
        self.sock = None
        if old is None:
            return
        try:
            old.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            old.close()
        except Exception:
            pass

    def start(self):
        """
        启动发送线程。

        <p><b>每次都必须重建队列 —— 这里踩过一个很典型的坑。</b>

        stop() 为了让卡在 get() 上的发送线程立刻醒来，会往两条队列里各丢一个
        None 当哨兵。如果 start() 沿用同一对队列对象，新线程拿到的第一个任务
        就是这个残留的 None，`if task is None: break` 当场退出 ——
        发送线程活了不到一毫秒，所有帧只进不出，socket 上一个字节都没有。

        <p>用户看到的现象正是：「第一次启动发送端可以正常投屏画面，
        但是点击停止投屏之后再次点击开始投屏，则无法正常传输画面，需要重启才行。」
        —— 因为界面、编码器、统计全都正常（码率在涨、队列在积压），
        只有接收端颗粒无收，极难从表象定位。

        <p>tools/restart_session_test.py 专门守着这条路径。
        """
        # 重建队列：把上一轮残留的 None 哨兵和过时帧一并丢掉
        self._qv = queue.Queue()
        self._qa = queue.Queue()
        self._stop.clear()
        self._generation += 1
        self.stats = TxStats()
        self._win_start = time.perf_counter()
        self._win_bytes = 0
        self._win_frames = 0
        # 丢帧状态机与拥塞节流是"上一次会话"的状态，必须清掉，
        # 否则新一轮一开局就处在一路丢帧等关键帧的状态里。
        self._drop_video_until_key = False
        self._last_congestion_ns = 0
        gen = self._generation
        self._thread = threading.Thread(target=self._loop, args=(gen,),
                                        name="castlink-tx", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        # 先让可能卡在 sendall 上的线程有个出路：对端不读时 TCP 窗口关闭，
        # sendall 会一直阻塞，光靠 join(timeout) 是收不回来的。
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        for q in (self._qv, self._qa):
            try:
                q.put_nowait(None)  # type: ignore[arg-type]
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        # 哨兵用完就清掉，不给下一轮留雷
        for q in (self._qv, self._qa):
            while True:
                try:
                    q.get_nowait()
                except Exception:
                    break

    def close(self):
        self.stop()
        self._drop_socket()


    # --------- 入队 ---------
    def put(self, task: FrameTask, drop_if_full: bool) -> bool:
        """
        非阻塞入队。**永远不会 sleep、永远不会阻塞采集线程。**

        drop_if_full 只是"用哪条上限"的开关：
          - True（低延迟 / UDP）：4 帧上限，最快反应；
          - False（可靠 / TCP）：6 帧上限，仍留一点无损模式的余量。
        两种模式都会丢帧 —— 这是实时投屏与文件传输的本质区别。
        """
        if task.stream == STREAM_AUDIO:
            # 音频帧彼此独立，可以**丢旧保新** —— 队满时挤掉最老的一帧，
            # 让最新的一帧立刻排上。反过来（拒收新帧、留着旧的）会让声音
            # 越拖越晚，而每一帧本身又都是"过时但仍然完整"的，听起来就是
            # 声音越来越滞后于画面。
            while self._qa.qsize() >= self.QUEUE_MAX_AUDIO:
                try:
                    self._qa.get_nowait()
                except queue.Empty:
                    break
                self.stats.dropped += 1
                self.stats.dropped_audio += 1
            self._qa.put_nowait(task)
            return True

        # ---- 视频 ----
        # 0) 延迟上限：这一帧已经在流水线里滞留太久，实时价值已经归零。
        #    关键帧例外 —— 它是接收端唯一的恢复手段，再老也得发。
        if (not task.keyframe and task.lag_us > self.LATENCY_CAP_US):
            self.stats.dropped += 1
            self.stats.dropped_video += 1
            self.stats.dropped_late += 1
            self._drop_video_until_key = True
            self._note_congestion(
                f"画面滞后 {task.lag_us / 1000:.0f}ms，丢帧追实时")
            return False

        limit = self.QUEUE_MAX_VIDEO if drop_if_full else self.QUEUE_MAX_VIDEO_RELIABLE

        if task.keyframe:
            # 关键帧是**唯一**的恢复点，无论什么状态都放行。
            #
            # <p>这里踩过一个很隐蔽的坑：老代码只在"队列已满时收到关键帧"
            # 才顺手把 `_drop_video_until_key` 清掉。于是当丢弃是因为
            # **延迟上限**（队列其实很短）而发生时，标志位会一直挂着 ——
            # 之后的普通帧继续被丢，而队里又永远凑不满，
            # 标志位就再也回不到 False。表现是画面退化成"只有关键帧"，
            # 也就是每 0.5 秒才动一下，比不修还糟。
            # 所以关键帧到达时**总是**退出丢帧状态。
            if self._drop_video_until_key or self._qv.qsize() >= limit:
                drained = self._drain_video()
                if drained:
                    self.stats.dropped += drained
                    self.stats.dropped_video += drained
                    self._note_congestion(f"队列积压，丢弃 {drained} 帧后从关键帧续传")
            self._drop_video_until_key = False
            self._qv.put_nowait(task)
            return True

        if self._drop_video_until_key:
            self.stats.dropped += 1
            self.stats.dropped_video += 1
            self._note_congestion("队列积压，丢帧等待关键帧")
            return False

        if self._qv.qsize() >= limit:
            # 丢到下一个关键帧为止：先丢掉已经过时的 P 帧，别再往队里塞。
            # 不能只丢当前这一帧 —— 队里那些同样过时的帧会把延迟继续撑住。
            self._drop_video_until_key = True
            self.stats.dropped += 1
            self.stats.dropped_video += 1
            self._note_congestion("队列积压，丢帧等待关键帧")
            return False

        self._qv.put_nowait(task)
        return True

    def _drain_video(self) -> int:
        n = 0
        while True:
            try:
                self._qv.get_nowait()
                n += 1
            except queue.Empty:
                return n

    def _note_congestion(self, why: str):
        """拥塞节流上报（1.5 秒最多一次），避免刷屏。"""
        cb = self.on_congestion
        if cb is None:
            return
        now = time.monotonic_ns()
        if now - self._last_congestion_ns < 1_500_000_000:
            return
        self._last_congestion_ns = now
        try:
            cb(why)
        except Exception:
            pass

    @property
    def queue_size(self) -> int:
        return self._qv.qsize() + self._qa.qsize()

    @property
    def alive(self) -> bool:
        """发送线程是否还活着。为 False 而 stats.frames 不涨 = 投屏已经哑了。"""
        return bool(self._thread and self._thread.is_alive())

    # --------- 发送循环 ---------
    def _loop(self, generation: int):
        # socket 与队列都绑定到"这一代"，这样即便有上一轮的僵尸线程还在，
        # 也绝不会把旧帧写进新一轮的连接里。
        sock = self.sock
        qv, qa = self._qv, self._qa
        while not self._stop.is_set() and generation == self._generation:
            # 音频优先：它码率极低，但迟到最容易被听出来。
            task = None
            try:
                task = qa.get_nowait()
            except queue.Empty:
                try:
                    task = qv.get(timeout=0.25)
                except queue.Empty:
                    continue
            if task is None:
                # 只有 stop() 会放哨兵进来。此时 _stop 已经置位，直接收摊。
                break
            frag = MAX_UDP_PAYLOAD if self.mode == MODE_UDP else MAX_TCP_PAYLOAD
            packets = pack_frame(task.stream, task.codec, task.frame_id,
                                 task.pts, task.payload, task.keyframe, frag)
            try:
                for pkt in packets:
                    if self.mode == MODE_UDP:
                        if sock and self.udp_addr:
                            sock.sendto(pkt, self.udp_addr)
                    else:
                        if sock:
                            sock.sendall(pkt)
                    self._win_bytes += len(pkt)
                self._win_frames += 1
                self.stats.bytes_total += sum(len(p) for p in packets)
                self.stats.frames_total += 1
            except Exception as e:
                if self._stop.is_set() or generation != self._generation:
                    break
                self.log(f"发送失败: {e}")
                break
            self._update_rate()

    def _update_rate(self):
        now = time.perf_counter()
        dt = now - self._win_start
        if dt >= 0.5:
            self.stats.bitrate_mbps = self._win_bytes * 8 / 1e6 / dt
            self.stats.fps = self._win_frames / dt
            self._win_bytes = 0
            self._win_frames = 0
            self._win_start = now
        self.stats.queued = self.queue_size

