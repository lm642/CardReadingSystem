# -*- coding: utf-8 -*-
"""
====================================================================
 uhf_reader.py  ——  KL9007U / UHFReader18 系列读写器 通信驱动库
====================================================================
 这是整个项目的"地基"：负责和读写器"说话"。

 为什么不能像普通门禁读卡器那样直接读串口？
   你手上这台是【UHF 超高频】读写器（KL9007U，协议类型 UHFReader18），
   出厂/演示软件里默认是【应答模式】—— 它不会主动往串口"吐"卡号，
   而是"你问一句，它答一句"。所以必须先按协议给它发"询查"命令，
   它才会把读到的标签 EPC 回给你。这就是"刷卡没反应"的根本原因。

--------------------------------------------------------------------
 一、物理层（串口参数）
   波特率 57600，1 起始位，8 数据位，1 停止位，无校验（8N1）
   字节按"最低有效位先发"（LSB first）

 二、帧格式（上位机 -> 读写器：命令帧）
   ┌─────┬─────┬─────┬───────┬────────┬────────┐
   │ Len │ Adr │ Cmd │ Data  │ CRC-L  │ CRC-H  │
   └─────┴─────┴─────┴───────┴────────┴────────┘
   Len = Data 长度 + 4（Adr1 + Cmd1 + CRC2），范围 4~96
   （读写器 -> 上位机：响应帧，多了 reCmd 与 Status 两个字节）
   ┌─────┬─────┬───────┬────────┬───────┬────────┬────────┐
   │ Len │ Adr │ reCmd │ Status │ Data  │ CRC-L  │ CRC-H  │
   └─────┴─────┴───────┴────────┴───────┴────────┴────────┘
   Len = Data 长度 + 5

 三、CRC16
   多项式 POLYNOMIAL = 0x8408，初值 PRESET_VALUE = 0xFFFF，
   结果不做异或，低字节在前。校验范围：从 Len 到 Data 的全部字节。

 四、本项目用到的关键命令
   0x21  读取读写器信息（用来"握手"，确认通信正常）
   0x01  询查（Inventory）—— 读标签的 EPC，就是我们的"卡号"
         不带参数        -> 直接返回 EPC
         带 AdrTID/LenTID -> 返回 TID（更唯一，但不同卡可能不通用）
   0x20  设置波特率（0x00=9600 0x01=19200 0x02=38400 0x03=57600 0x04=115200）

--------------------------------------------------------------------
 依赖：pyserial（已内置在项目 _vendor 目录，无需 pip install）
 单独自检（不需要硬件）：python uhf_reader.py
====================================================================
"""

from __future__ import annotations

import os
import sys
import time
import threading

# --------------------------------------------------------------------
# 串口库（pyserial）加载：先找系统里 pip 装好的，找不到就用项目内置的 _vendor。
# 为什么要这样做？——一台电脑上常装有多个 Python，pip 只装进了其中一个，
# 而程序可能是用另一个 Python 启动的，于是"明明装了却报没装"。
# 把 pyserial 放进项目自带的 _vendor 目录，任何 Python 都能直接读到。
# --------------------------------------------------------------------
VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor")


def _import_serial():
    for use_vendor in (False, True):
        if use_vendor:
            if not os.path.isdir(os.path.join(VENDOR_DIR, "serial")):
                continue
            if VENDOR_DIR not in sys.path:
                sys.path.insert(0, VENDOR_DIR)
        try:
            import serial as _s
            import serial.tools.list_ports  # noqa: F401
            return _s
        except Exception:
            continue
    return None


serial = _import_serial()
HAS_SERIAL = serial is not None
SERIAL_SOURCE = ("项目内置 _vendor" if (HAS_SERIAL and
                 getattr(serial, "__file__", "").startswith(VENDOR_DIR))
                 else ("系统已安装 pyserial" if HAS_SERIAL else "未找到"))


# ====================================================================
# 一、CRC16 与组帧（纯函数，无副作用，方便单独测试）
# ====================================================================

def crc16(data: bytes) -> int:
    """
    UHFReader18 使用的 CRC-16（多项式 0x8408，初值 0xFFFF，无输出异或）。
    等价于国际标准 CRC-16/MCRF4XX。
    """
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc & 0xFFFF


def build_command(adr: int, cmd: int, data: bytes = b"") -> bytes:
    """把 (地址, 命令, 数据) 打包成一条完整的命令帧。"""
    body = bytes([len(data) + 4, adr, cmd]) + data     # Len,Adr,Cmd,Data
    c = crc16(body)
    return body + bytes([c & 0xFF, (c >> 8) & 0xFF])   # 追加 CRC（低字节在前）


def split_frames(buf: bytes):
    """
    从"字节流"里切出完整的响应帧（解决串口粘包 / 半包问题）。

    做法：以第一个字节 Len 作为帧长依据（整帧 = 1 + Len 字节），
    再用 CRC 校验；校验不过就后移一个字节重新找帧头（重新同步）。
    返回 (已识别的帧列表, 剩余不足一帧的字节)。
    """
    frames = []
    i, n = 0, len(buf)
    while i < n:
        if n - i < 2:
            break                                  # 连 Len 都不够
        ln = buf[i]
        total = 1 + ln
        if ln < 5 or ln > 100:                     # 长度不合理 -> 不是帧头
            i += 1
            continue
        if n - i < total:                          # 数据还没收全，等下一批
            break
        cand = buf[i:i + total]
        if crc16(cand[:-2]) == (cand[-2] | (cand[-1] << 8)):
            frames.append(cand)
            i += total
        else:
            i += 1                                 # CRC 不过 -> 后移一字节重新同步
    return frames, buf[i:]


# ====================================================================
# 二、驱动器本体
# ====================================================================

# 命令码常量（用名字代替魔法数字，答辩时更好讲）
CMD_INVENTORY   = 0x01   # 询查（读 EPC / TID）
CMD_READ_DATA   = 0x02   # 读标签存储区
CMD_GET_INFO    = 0x21   # 读取读写器信息
CMD_SET_BAUD    = 0x20   # 设置波特率

STATUS_OK = {
    0x00: "操作成功",
    0x01: "询查完成，返回标签 EPC",
    0x02: "询查时间到，强制返回已读到的标签",
    0x03: "标签过多，分多条返回",
    0x04: "标签过多，超出缓存，返回已读取部分",
    0xFD: "参数错误（命令数据长度不符）",
    0xFE: "无法识别的命令，或命令 CRC 校验错误",
}


class UHFReaderError(Exception):
    """读写器通信相关异常的统一类型，方便上层 catch。"""


class UHFReader18:
    """
    KL9007U / UHFReader18 读写器驱动。

    最典型的用法：
        r = UHFReader18("COM9", 57600)
        r.open()
        print(r.get_reader_info())     # 握手，确认设备在线
        while True:
            print(r.inventory())       # 循环询查，返回 ['001158230000', ...]
    """

    def __init__(self, port: str, baud: int = 57600, adr: int = 0x00,
                 timeout: float = 1.2, verbose: bool = False,
                 on_log=None):
        """
        :param port:    串口号，如 "COM9"
        :param baud:    波特率，默认 57600（与读写器一致）
        :param adr:     读写器地址，出厂默认 0x00；用 0xFF 表示广播地址
        :param timeout: 单条命令的最长等待时间（秒）。
                        读写器默认询查时间约 1 秒，所以这里要留够。
        :param verbose: 是否把收发原始字节打到日志（诊断用）
        :param on_log:  自定义日志回调 on_log(text)，不传则 print
        """
        self.port = port
        self.baud = int(baud)
        self.adr = int(adr)
        self.timeout = float(timeout)
        self.verbose = verbose
        self.on_log = on_log
        self.ser = None
        self._buf = b""

    # ---------------- 日志 ----------------
    def _log(self, text: str):
        if self.on_log:
            self.on_log(text)
        elif self.verbose:
            print(text)

    @staticmethod
    def _hex(b: bytes) -> str:
        return " ".join(f"{x:02X}" for x in b)

    # ---------------- 串口开关 ----------------
    def open(self):
        if not HAS_SERIAL:
            raise UHFReaderError(
                "串口库 pyserial 不可用。请确认项目目录下存在 _vendor\\serial 文件夹"
                "（整个项目要完整解压后再运行）；或在主程序【设置】页点"
                "「一键安装 pyserial」联网安装。")
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
        except Exception as e:
            raise UHFReaderError(f"打开串口 {self.port} 失败：{e}") from e
        self._buf = b""
        # 清掉打开瞬间可能残留的脏数据
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception:
            pass
        self._log(f"[串口] 已打开 {self.port} @ {self.baud} 8N1")
        return True

    def close(self):
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    @property
    def is_open(self) -> bool:
        return bool(self.ser and self.ser.is_open)

    # ---------------- 收发 ----------------
    def _write(self, frame: bytes):
        if not self.is_open:
            raise UHFReaderError("串口未打开")
        self._log(f"[发送] {self._hex(frame)}")
        try:
            self.ser.write(frame)
            self.ser.flush()
        except Exception as e:
            raise UHFReaderError(f"串口写入失败（设备被拔掉？）：{e}") from e

    def _read_frames(self, deadline: float):
        """在 deadline 之前，尽力从串口读出若干完整帧。"""
        out = []
        while time.time() < deadline:
            try:
                chunk = self.ser.read(self.ser.in_waiting or 1)
            except Exception as e:
                raise UHFReaderError(f"串口读取失败（设备被拔掉？）：{e}") from e
            if chunk:
                self._buf += chunk
                frames, self._buf = split_frames(self._buf)
                for f in frames:
                    self._log(f"[接收] {self._hex(f)}")
                    out.append(f)
            else:
                time.sleep(0.01)
        return out

    def transact(self, cmd: int, data: bytes = b"", timeout: float | None = None):
        """
        发一条命令，等它的应答帧。返回响应帧 bytes，超时/无应答返回 None。
        响应帧结构：Len Adr reCmd Status Data... CRC-L CRC-H
        """
        frame = build_command(self.adr, cmd, data)
        self._write(frame)
        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        for f in self._read_frames(deadline):
            # reCmd 等于我们发的命令，才算这一条的应答
            if f[2] == cmd:
                return f
        return None

    # ---------------- 高层命令 ----------------
    @staticmethod
    def status_text(status: int) -> str:
        return STATUS_OK.get(status, f"未知状态 0x{status:02X}")

    def get_reader_info(self) -> dict | None:
        """
        读取读写器信息 —— 相当于"握手"，用来确认串口、波特率、地址都对了。
        返回字典；无应答返回 None。
        """
        f = self.transact(CMD_GET_INFO)
        if f is None:
            return None
        if f[3] != 0x00:
            self._log(f"[信息] 返回异常状态：{self.status_text(f[3])}")
            return None
        d = f[4:-2]
        if len(d) < 8:
            return {"raw": d.hex().upper()}
        # 解析频率：bit7~bit6 是频段，bit5~bit0 是频率点
        dmax, dmin = d[4], d[5]
        band_code = (dmax >> 6) & 0x03
        band_name = {0: "User band", 1: "Chinese band2", 2: "US band", 3: "Korean band"}.get(band_code)
        return {
            "version":  f"{d[0]}.{d[1]}",
            "type":     d[2],                       # 0x09 = UHFREADER18
            "tr_type":  d[3],                       # bit1 支持 6C，bit0 支持 6B
            "support_6c": bool(d[3] & 0x02),
            "support_6b": bool(d[3] & 0x01),
            "band":     band_name,
            "power":    d[6],
            "scan_time_100ms": d[7],
        }

    def inventory(self, adr_tid: int | None = None, len_tid: int | None = None,
                  timeout: float | None = None):
        """
        询查标签。返回 EPC（或 TID）字符串列表。

        - 不传 adr_tid/len_tid  -> 读 EPC（本项目默认，作为"卡号"）
        - 传 adr_tid / len_tid  -> 读 TID（例如 adr_tid=2, len_tid=4）

        返回 None 表示"通信失败/无应答"（上层据此判断读卡器掉线）；
        返回 []   表示"通信正常，但当前没有检测到标签"。
        """
        data = b""
        if adr_tid is not None and len_tid is not None:
            data = bytes([adr_tid & 0xFF, len_tid & 0xFF])
        f = self.transact(CMD_INVENTORY, data, timeout=timeout or max(self.timeout, 1.5))
        if f is None:
            return None
        status = f[3]
        payload = f[4:-2]
        if status in (0xFD, 0xFE):
            self._log(f"[询查] 读写器返回错误：{self.status_text(status)}")
            return []
        return parse_inventory_payload(payload)

    # ---------------- 常用配置命令（可选，非必需） ----------------
    def set_baud_rate(self, baud: int):
        """
        设置读写器波特率（改完读写器会立刻用新波特率通信，需要重开串口）。
        码表：0x00=9600 0x01=19200 0x02=38400 0x03=57600 0x04=115200
        """
        table = {9600: 0x00, 19200: 0x01, 38400: 0x02, 57600: 0x03, 115200: 0x04}
        if baud not in table:
            raise UHFReaderError(f"不支持的波特率：{baud}")
        return self.transact(CMD_SET_BAUD, bytes([table[baud]]))

    def set_power(self, power: int):
        """设置发射功率（0~30）。命令 0x2F。"""
        return self.transact(0x2F, bytes([max(0, min(30, int(power)))]))


# ====================================================================
# 三、响应解析（单独抽出来，方便写单元测试）
# ====================================================================

def parse_inventory_payload(payload: bytes):
    """
    解析询查响应里的 Data 段，取出 EPC/TID 列表。

    官方文档格式（格式 A）：Data = [标签个数 Num][EPC长度1][EPC1][EPC长度2][EPC2]...
    但市面上固件版本较多，为稳妥起见做了兼容（格式 B）：Data = [Num][EPC]。
    """
    if not payload:
        return []

    # ---- 格式 A：严格按 (个数 + 长度前缀) 解析，必须刚好把 payload 用完 ----
    if payload[0] <= 32:
        num = payload[0]
        pos, tags = 1, []
        ok = True
        for _ in range(num):
            if pos >= len(payload):
                ok = False
                break
            l = payload[pos]
            pos += 1
            if l == 0 or pos + l > len(payload):
                ok = False
                break
            tags.append(payload[pos:pos + l].hex().upper())
            pos += l
        if ok and pos == len(payload) and len(tags) == num:
            return tags

    # ---- 格式 B：只有 1 个标签，Data = [Num][EPC...] ----
    if payload[0] == 1 and len(payload) > 1:
        return [payload[1:].hex().upper()]

    # ---- 兜底：整段当作 EPC ----
    return [payload.hex().upper()]


def parse_response_frame(frame: bytes):
    """把一帧响应拆成字典，供诊断工具展示。"""
    if len(frame) < 6:
        return None
    return {
        "len":    frame[0],
        "adr":    frame[1],
        "recmd":  frame[2],
        "status": frame[3],
        "status_text": UHFReader18.status_text(frame[3]),
        "data":   frame[4:-2].hex(" ").upper(),
        "crc_ok": crc16(frame[:-2]) == (frame[-2] | (frame[-1] << 8)),
    }


# ====================================================================
# 四、后台读卡线程（给 GUI 用）：循环询查 + 去抖 + 断线自动重连
# ====================================================================

class CardReaderThread(threading.Thread):
    """
    在后台线程里循环"询查 -> 上报卡号"，界面线程只管从 msg_queue 取结果。

    设计要点（答辩考点：拔掉读卡器会怎样？重复刷卡怎么办？）：
      * 读卡器被拔掉 -> 串口读写抛异常 -> 捕获后不退出，提示并重连；
      * 同一张卡在 debounce 秒内反复被读到 -> 只上报一次（防重复录入）；
      * 卡号不变（卡一直在读卡器上）-> 到期前一直忽略，实现"拿走再刷才算一次"。
    """

    def __init__(self, port, baud, adr, msg_queue,
                 poll_interval=0.15, debounce=3.0, reconnect=3.0,
                 use_tid=False, verbose=False):
        super().__init__(daemon=True)
        self.port, self.baud, self.adr = port, int(baud), int(adr)
        self.q = msg_queue
        self.poll_interval = float(poll_interval)
        self.debounce = float(debounce)
        self.reconnect = float(reconnect)
        self.use_tid = use_tid
        self.verbose = verbose
        self._stop = threading.Event()
        self.read_count = 0
        self.reader: UHFReader18 | None = None

    def stop(self):
        self._stop.set()
        if self.reader:
            self.reader.close()

    def _status(self, text, level="info"):
        self.q.put(("status", text, level))

    def run(self):
        while not self._stop.is_set():
            try:
                reader = UHFReader18(self.port, self.baud, self.adr,
                                     verbose=self.verbose)
                reader.open()
                self.reader = reader

                info = reader.get_reader_info()
                if info:
                    self._status(f"读卡器已连接 {self.port}@{self.baud}，"
                                 f"型号类型 {info.get('type')}，"
                                 f"版本 {info.get('version')}，功率 {info.get('power')}", "ok")
                else:
                    self._status(f"串口已打开，但读卡器无应答（检查波特率/地址）", "warn")

                last_emit: dict[str, float] = {}
                last_poll = 0.0
                while not self._stop.is_set():
                    now = time.time()
                    if now - last_poll >= self.poll_interval:
                        last_poll = now
                        tags = (reader.inventory(2, 4) if self.use_tid
                                else reader.inventory())
                        if tags is None:
                            raise UHFReaderError("询查无应答")
                        for epc in tags:
                            if now - last_emit.get(epc, 0) >= self.debounce:
                                last_emit[epc] = now
                                self.read_count += 1
                                self.q.put(("card", epc))
                    else:
                        time.sleep(0.01)

                    # 清理过期的去抖记录，避免字典无限变大
                    if len(last_emit) > 200:
                        last_emit = {k: v for k, v in last_emit.items()
                                     if now - v < self.debounce}

            except Exception as e:
                self._status(f"读卡器异常：{e}；{self.reconnect:.0f} 秒后自动重连…", "err")
                if self.reader:
                    self.reader.close()
                self.reader = None
                waited = 0.0
                while waited < self.reconnect and not self._stop.is_set():
                    time.sleep(0.1)
                    waited += 0.1


# ====================================================================
# 五、串口辅助 + 模块自检
# ====================================================================

def list_serial_ports():
    """列出系统串口，返回 [(设备名, 描述, 厂商), ...]"""
    if not HAS_SERIAL:
        return []
    out = []
    for p in serial.tools.list_ports.comports():
        out.append((p.device, p.description or "", p.manufacturer or ""))
    return out


def guess_reader_port() -> str:
    """猜一个"像读写器"的串口（CH340/CH341/CP210x/PL2303/FT232 等）。"""
    keys = ("CH340", "CH341", "CP210", "PL2303", "FT232", "USB-SERIAL",
            "Silicon Labs", "USB Serial", "UART")
    ports = list_serial_ports()
    for dev, desc, man in ports:
        text = f"{desc} {man}".upper()
        if any(k in text for k in keys):
            return dev
    return ports[0][0] if ports else ""


# --------------------------------------------------------------------
# 自检：不需要硬件，验证 CRC / 组帧 / 解析是否正确
# python uhf_reader.py
# --------------------------------------------------------------------
def _selftest():
    print("=" * 60)
    print("uhf_reader 协议层自检")
    print("=" * 60)
    ok = 0
    total = 0

    def check(desc, expect, actual):
        nonlocal ok, total
        total += 1
        good = (expect == actual)
        ok += good
        print(f"[{'PASS' if good else 'FAIL'}] {desc}")
        if not good:
            print(f"        期望 {expect!r}")
            print(f"        实际 {actual!r}")

    # 1) 公开资料中 UHFReader18 的询查命令固定为 04 00 01 DB 4B
    check("询查指令帧 = 04 00 01 DB 4B",
          "040001DB4B", build_command(0x00, 0x01).hex().upper())
    # 2) 读取读写器信息命令
    check("读信息指令帧 = 04 00 21 D9 6A",
          "040021D96A", build_command(0x00, 0x21).hex().upper())
    # 3) 读 TID（起始字 2，读 4 个字）
    check("读TID指令帧 = 06 00 01 02 04 58 24",
          "06000102045824", build_command(0x00, 0x01, b"\x02\x04").hex().upper())

    # 4) CRC 往返
    f = build_command(0x00, 0x01)
    check("CRC 自校验通过", True, crc16(f[:-2]) == (f[-2] | (f[-1] << 8)))

    # 5) 切帧：粘包（两帧连在一起）+ 前面有垃圾字节
    def _mk_resp(recmd, status, data=b""):
        body = bytes([len(data) + 5, 0x00, recmd, status]) + data
        c = crc16(body)
        return body + bytes([c & 0xFF, (c >> 8) & 0xFF])

    frame = _mk_resp(0x01, 0x01, bytes([0x01, 0x06, 0, 0x11, 0x58, 0x23, 0, 0]))
    stream = b"\xAA\xBB" + frame + frame
    frames, rest = split_frames(stream)
    check("粘包/脏数据下能切出 2 帧", 2, len(frames))
    # 5b) 半包：只收到半帧，不该当成完整帧
    frames2, rest2 = split_frames(frame[:4])
    check("半包不误判为完整帧", 0, len(frames2))

    # 6) 响应解析：格式 A  [Num][Len][EPC]
    payload = bytes([0x01, 0x06, 0x00, 0x11, 0x58, 0x23, 0x00, 0x00])
    check("解析格式A -> ['001158230000']", ["001158230000"],
          parse_inventory_payload(payload))
    # 7) 响应解析：无标签
    check("解析无标签 -> []", [], parse_inventory_payload(bytes([0x00])))
    # 8) 响应解析：格式 B  [Num][EPC]
    check("解析格式B -> ['A1B2C3D4']", ["A1B2C3D4"],
          parse_inventory_payload(bytes([0x01, 0xA1, 0xB2, 0xC3, 0xD4])))

    print("-" * 60)
    print(f"自检结果：{ok}/{total} 通过")
    return ok == total


if __name__ == "__main__":
    _selftest()
