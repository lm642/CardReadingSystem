# -*- coding: utf-8 -*-
"""
====================================================================
 RFID 学生签到 / 签退系统        v2.0（适配 KL9007U / UHFReader18）
 ------------------------------------------------------------------
 硬件       : KL9007U 超高频(UHF)读写器，串口通信 COM9 @ 57600 8N1
 运行环境   : Python 3.8+ / Windows
 界面       : tkinter（Python 自带，无需安装）
 数据库     : SQLite（Python 自带 sqlite3，免安装、免配置）
 串口驱动   : pyserial —— 已内置在项目自带的 _vendor 目录里，开箱即用，
              不需要 pip install，也不需要联网（见 §六 说明）。

 启动       : 双击「启动程序.bat」，或 python attendance.py
 一键读卡测试: python read_test.py            （先确认硬件能读到卡）
 查看串口   : python attendance.py --list-ports
 运行自检测试: python attendance.py --selftest

 ------------------------------------------------------------------
 为什么是"应答模式"？—— 重要，答辩会问
   本读写器是 UHF 超高频读写器（协议 UHFReader18），出厂默认【应答模式】：
   它不会像普通门禁读卡器那样主动往串口吐卡号，而是"上位机发一条询查命令，
   读写器回一条标签数据"。所以代码里必须先按协议组帧、算 CRC、发命令，
   再把响应帧解析成 EPC 卡号 —— 这段逻辑全部封装在同目录 uhf_reader.py。

 ------------------------------------------------------------------
 代码结构（答辩时按这个顺序讲，逻辑最清晰）：

   [1] 常量与配置        全局颜色、默认参数
   [2] 卡号处理          normalize_card()  把读卡器原始字节流 -> 标准卡号
   [3] 数据访问层 DAO    Database 类        建表 / 增删改查 / 设置项
   [4] 设备驱动层        SerialCardReader   串口读卡线程（内部调用 uhf_reader
                        的协议驱动；含断线自动重连、防重复）
   [5] 业务逻辑层        AttendanceService  签到 / 签退 判定 + 防重复
   [6] 界面层            AppUI              tkinter 图形界面
   [7] 自检测试          run_selftest()     覆盖正常 + 异常用例
====================================================================
"""

from __future__ import annotations

import os
import re
import sys
import csv
import time
import queue
import sqlite3
import subprocess
import threading
import datetime as _dt
from datetime import datetime

# --------------------------------------------------------------------
# [0] 依赖与"内置依赖"兜底（重要，答辩会问）
#
#     问题：一台电脑上常常装了多个 Python（比如同时有 3.12 / 3.13 / 3.14），
#           pip install pyserial 只装进了"某一个"，而程序却可能是用
#           "另一个" Python 启动的 —— 于是明明装过，程序却报"未安装"。
#
#     解决：把 pyserial 整个源码放进项目自带的 _vendor 目录，作为"内置依赖"。
#           程序启动时先尝试 import serial；失败则把 _vendor 加到模块搜索
#           路径的最前面再试一次。这样无论用哪个 Python 启动，都能读到串口，
#           不需要联网、不需要 pip、不需要管理员权限。
#           —— 这是典型的"依赖内置(vendoring)"做法，也是本项目的第一处容错。
# --------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(BASE_DIR, "_vendor")


def _ensure_vendor_on_path():
    """把项目自带的 _vendor 目录挂到模块搜索路径最前面（只用一次）。"""
    if os.path.isdir(os.path.join(VENDOR_DIR, "serial")) and VENDOR_DIR not in sys.path:
        sys.path.insert(0, VENDOR_DIR)
        return True
    return False


def _import_serial():
    """返回 (serial, list_ports, 来源说明)。先系统安装的，再内置的。"""
    for src, hook in (("系统已安装", False), ("项目内置 _vendor", True)):
        if hook:
            _ensure_vendor_on_path()
        try:
            import serial as _s
            from serial.tools import list_ports as _lp
            return _s, _lp, src
        except Exception:
            continue
    return None, None, ""


try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, simpledialog
    HAS_TK = True
except Exception:                       # 无图形环境（例如纯命令行自检）
    tk = None
    ttk = messagebox = filedialog = simpledialog = None
    HAS_TK = False

serial, list_ports, SERIAL_FROM = _import_serial()
HAS_SERIAL = serial is not None


def reload_serial():
    """
    重新尝试加载串口库。
    用在：用户刚 pip 装好、或把项目里 _vendor/serial 目录补回来之后，
    不重启程序也能让串口模块立即可用。
    """
    global serial, list_ports, SERIAL_FROM, HAS_SERIAL
    s, lp, src = _import_serial()
    if s is not None:
        serial, list_ports, SERIAL_FROM, HAS_SERIAL = s, lp, src, True
    return HAS_SERIAL

try:
    import winsound                     # Windows 蜂鸣反馈（非必须）
except Exception:
    winsound = None

# 项目自己的协议驱动库（KL9007U / UHFReader18）——与主程序放在同一目录
try:
    import uhf_reader
except Exception as _e:                 # 理论上不会失败，但保险起见不直接崩
    uhf_reader = None
    _UHF_IMPORT_ERROR = _e
else:
    _UHF_IMPORT_ERROR = None


# ====================================================================
# [1] 常量与配置
# ====================================================================

APP_NAME = "RFID 学生签到签退系统"
APP_VERSION = "2.1"

# 说明：BASE_DIR / VENDOR_DIR 已在文件开头的"依赖兜底"处定义（第 66~67 行），
#       这里直接复用，避免出现两个含义相同的路径变量。
DB_PATH = os.path.join(BASE_DIR, "attendance.db")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")

# 界面配色（浅色主题）
C_BG      = "#F5F7FA"   # 窗口底色
C_PANEL   = "#FFFFFF"   # 卡片底色
C_BORDER  = "#DCE3EC"
C_PRIMARY = "#2F6FED"   # 主色（蓝）
C_IN      = "#1F9D55"   # 签到 —— 绿
C_OUT     = "#E08A1E"   # 签退 —— 橙
C_ERR     = "#D64545"   # 异常 —— 红
C_TXT     = "#1F2937"   # 正文
C_SUB     = "#6B7280"   # 次要文字

# 设置项默认值（存库，可在界面上改，不需要改代码）
# 注意：默认值已按你的实训硬件 KL9007U 配好（COM9 / 57600 / 应答模式）
DEFAULT_SETTINGS = {
    "interval_minutes": "30",       # 【核心规则】签到与签退的最小间隔（分钟）
    "debounce_seconds": "3",        # 防重复刷卡：同一张卡的忽略窗口（秒）
    "reader_mode":      "serial",   # serial=串口主动询查 | keyboard=键盘模拟
    "serial_port":      "COM9",     # 读写器串口（设备管理器里确认）
    "serial_baud":      "57600",    # KL9007U 默认 57600（可选 9600/19200/115200）
    "reader_adr":       "0",        # 读写器地址，出厂 0；读不到可试 255(广播)
    "use_tid":          "0",        # 0=读 EPC 当卡号；1=读 TID（更唯一）
    "poll_interval":    "0.15",     # 询查间隔（秒），越小越灵敏
    "reconnect_seconds": "3",       # 串口断线后的重连间隔（秒）
}

# 卡号合法性规则：4~32 位 十六进制字符或纯数字
# 为什么上限取 32：
#   高频 Mifare 卡 UID   -> 4 字节 = 8 位，或 7 字节 = 14 位
#   超高频 EPC Gen2 标签 -> EPC 96 位 = 24 个十六进制字符
#                           EPC 128 位 = 32 个十六进制字符
#   （KL9007U 这类 UHF 读写器读出来的就是 EPC 码，位数明显比门禁卡长）
CARD_RE = re.compile(r"^[0-9A-F]{4,32}$")


# ====================================================================
# [2] 卡号处理 —— 这是答辩最容易被问的一段
#     读卡器给出来的原始数据往往很"脏"：
#       b'\x02 E0 4A 1B 2C \r\n'     有帧头帧尾、有空格
#       "e04a1b2c\r\n"               小写、带回车换行
#       " 0017836532 "               前后空格
#     必须先归一化，否则同一张卡会算出两个不同的卡号，签到匹配必然失败。
# ====================================================================

def normalize_card(raw) -> str:
    """
    把读卡器的原始输出归一化成标准卡号。

    处理步骤（顺序不能乱）：
      1. bytes -> str：先按 ASCII 解码（绝大多数读卡器输出 ASCII 字符）；
         解不出来说明是二进制裸数据，直接转成大写十六进制字符串。
      2. 丢掉所有不可打印字符 -> 干掉 \r \n \x02 \x03 这类帧控制符。
      3. 丢掉空格、冒号、横线、逗号等分隔符 -> "E0 4A-1B:2C" 变成 "E04A1B2C"。
      4. 统一转大写 -> 避免 e04a1b2c 与 E04A1B2C 被当成两张卡。
      5. 去掉可能存在的 "0X" 前缀。

    注意：这里【不做】十六进制转十进制的换算。
    读卡器输出什么就用什么作为卡号主键，"所见即所得"，
    避免进制换算带来误差导致匹配失败（这是刻意的设计选择）。
    """
    if raw is None:
        return ""

    if isinstance(raw, (bytes, bytearray)):
        try:
            s = bytes(raw).decode("ascii")
        except UnicodeDecodeError:
            return bytes(raw).hex().upper()
    else:
        s = str(raw)

    # 步骤 2：过滤控制字符
    s = "".join(ch for ch in s if ch.isprintable())
    # 步骤 3：过滤分隔符
    s = re.sub(r"[\s:;\-_,\.]+", "", s)
    # 步骤 4：统一大写
    s = s.upper()
    # 步骤 5：去 0X 前缀
    if s.startswith("0X"):
        s = s[2:]
    return s


def is_valid_card(card: str) -> bool:
    """卡号合法性校验 —— 用于过滤明显的脏数据（半截数据、乱码等）。"""
    return bool(card) and bool(CARD_RE.match(card))


def parse_ts(ts: str) -> datetime:
    """把数据库里的时间字符串还原成 datetime 对象。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return datetime.now()


# ====================================================================
# [3] 数据访问层（DAO）—— 数据库表结构与全部 SQL 都集中在这里
# ====================================================================

SCHEMA_SQL = """
-- 学生表：主键 id（自增代理主键），业务主键 student_no（学号，唯一）
--         card_no 加唯一约束，保证一张卡只能绑定一个学生
CREATE TABLE IF NOT EXISTS students (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_no  TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    class_name  TEXT,
    card_no     TEXT    UNIQUE,
    created_at  TEXT    NOT NULL
);

-- 打卡记录表：主键 id（自增）
-- 关键设计：除了 student_id 外，还把 学号/姓名/班级 以"快照"方式冗余存一份。
-- 这样即使以后学生被删除或改了名字，历史记录依然能完整显示，不会变成空白。
CREATE TABLE IF NOT EXISTS attendance (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id   INTEGER,
    student_no   TEXT,
    student_name TEXT,
    class_name   TEXT,
    card_no      TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('in', 'out')),
    ts           TEXT NOT NULL,          -- 完整时间：2026-10-15 20:16:52
    day          TEXT NOT NULL           -- 日期：2026-10-15（便于按天查询、建索引）
);

-- 陌生卡记录表：空白卡/未绑定卡刷到时留痕，方便排查，也用于异常用例举证
CREATE TABLE IF NOT EXISTS unknown_cards (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    card_no TEXT NOT NULL,
    ts      TEXT NOT NULL
);

-- 设置表：key-value 结构，界面上的配置项都存在这里
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 索引：按天查询、按"学生+天"查最近一次记录，都走索引
CREATE INDEX IF NOT EXISTS idx_att_day      ON attendance(day);
CREATE INDEX IF NOT EXISTS idx_att_stu_day  ON attendance(student_id, day);
CREATE INDEX IF NOT EXISTS idx_stu_card     ON students(card_no);
"""


class Database:
    """所有数据库操作都在这个类里，界面层不直接写 SQL。"""

    def __init__(self, path: str | None = None):
        self.path = path or DB_PATH
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # check_same_thread=False：允许其它线程调用（本项目的写入都在主线程，
        # 这样写只是为了避免线程报错，兼容性更稳）
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row          # 查询结果可以按列名取值
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_SQL)
        self._init_settings()
        self.conn.commit()

    # ---------- 设置项 ----------
    def _init_settings(self):
        for k, v in DEFAULT_SETTINGS.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))

    def get_setting(self, key: str, default=""):
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))
        self.conn.commit()

    def get_float(self, key: str, default: float) -> float:
        try:
            return float(self.get_setting(key, str(default)))
        except (TypeError, ValueError):
            return default

    # ---------- 学生 ----------
    def list_students(self, keyword: str = ""):
        if keyword:
            kw = f"%{keyword}%"
            return self.conn.execute(
                "SELECT * FROM students "
                "WHERE student_no LIKE ? OR name LIKE ? OR class_name LIKE ? OR card_no LIKE ? "
                "ORDER BY student_no", (kw, kw, kw, kw)).fetchall()
        return self.conn.execute(
            "SELECT * FROM students ORDER BY student_no").fetchall()

    def get_student_by_card(self, card_no: str):
        return self.conn.execute(
            "SELECT * FROM students WHERE card_no = ?", (card_no,)).fetchone()

    def get_student_by_no(self, student_no: str):
        return self.conn.execute(
            "SELECT * FROM students WHERE student_no = ?", (student_no,)).fetchone()

    def add_student(self, student_no, name, class_name="", card_no=""):
        self.conn.execute(
            "INSERT INTO students(student_no, name, class_name, card_no, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (student_no.strip(), name.strip(), class_name.strip(),
             (card_no or "").strip().upper() or None,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self.conn.commit()

    def update_student(self, sid, student_no, name, class_name, card_no):
        self.conn.execute(
            "UPDATE students SET student_no=?, name=?, class_name=?, card_no=? WHERE id=?",
            (student_no.strip(), name.strip(), class_name.strip(),
             (card_no or "").strip().upper() or None, sid))
        self.conn.commit()

    def delete_student(self, sid):
        self.conn.execute("DELETE FROM students WHERE id = ?", (sid,))
        self.conn.commit()

    def count_students(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]

    # ---------- 打卡记录 ----------
    def add_record(self, stu, card_no: str, action: str, when: datetime) -> int:
        cur = self.conn.execute(
            "INSERT INTO attendance(student_id, student_no, student_name, class_name,"
            " card_no, action, ts, day) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (stu["id"], stu["student_no"], stu["name"], stu["class_name"],
             card_no, action,
             when.strftime("%Y-%m-%d %H:%M:%S"), when.strftime("%Y-%m-%d")))
        self.conn.commit()
        return cur.lastrowid

    def latest_record_of_day(self, student_id: int, day: str):
        """取某学生某天的最后一条记录 —— 判断签到还是签退就靠这一条。"""
        return self.conn.execute(
            "SELECT * FROM attendance WHERE student_id = ? AND day = ? "
            "ORDER BY ts DESC, id DESC LIMIT 1", (student_id, day)).fetchone()

    def query_records(self, day: str = "", keyword: str = "", limit: int = 1000):
        sql = ("SELECT * FROM attendance WHERE 1=1")
        args = []
        if day:
            sql += " AND day = ?"
            args.append(day)
        if keyword:
            sql += " AND (student_no LIKE ? OR student_name LIKE ? OR card_no LIKE ?)"
            kw = f"%{keyword}%"
            args += [kw, kw, kw]
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def today_stats(self, day: str):
        """今日概览：签到人数、签退人数、记录条数。"""
        r = self.conn.execute(
            "SELECT "
            " COUNT(DISTINCT CASE WHEN action='in'  THEN student_id END) AS in_cnt, "
            " COUNT(DISTINCT CASE WHEN action='out' THEN student_id END) AS out_cnt, "
            " COUNT(*) AS total FROM attendance WHERE day = ?", (day,)).fetchone()
        return {"in": r["in_cnt"], "out": r["out_cnt"], "total": r["total"]}

    # ---------- 陌生卡 ----------
    def log_unknown(self, card_no: str, when: datetime):
        self.conn.execute(
            "INSERT INTO unknown_cards(card_no, ts) VALUES (?, ?)",
            (card_no, when.strftime("%Y-%m-%d %H:%M:%S")))
        self.conn.commit()

    def list_unknown(self, limit: int = 200):
        return self.conn.execute(
            "SELECT * FROM unknown_cards ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ====================================================================
# [4] 设备驱动层
# ====================================================================

class SerialCardReader:
    """
    KL9007U / UHFReader18 读写器的读卡线程（应答模式）。

    设计要点（答辩考点：读卡器断开怎么办？重复刷卡怎么办？）：
      * 真正的协议细节（组帧、CRC、询查命令、响应解析）都在 uhf_reader.py 里，
        本类只负责"按界面层的调用习惯"包一层 —— 分工清晰，便于讲解与维护。
      * 独立线程循环"询查"，绝不阻塞界面线程 —— 界面永远不卡死。
      * 读卡器被拔掉 -> 串口读写抛异常 -> uhf_reader 捕获后不退出，提示并自动重连，
        实现"拔插自恢复"（reconnect_seconds 秒重试一次）。
      * 同一张卡在 debounce_seconds 内反复读到只上报一次，防止一次刷卡录多条。
      * 线程不直接操作界面（tkinter 非线程安全），结果统一丢进线程安全队列，
        由界面主线程轮询消费。
    """

    def __init__(self, port: str, baud: int, out_queue: "queue.Queue",
                 reconnect_sec: float = 3.0, adr: int = 0,
                 use_tid: bool = False, poll_interval: float = 0.15,
                 debounce_sec: float = 3.0):
        self.q = out_queue
        if uhf_reader is None:
            raise RuntimeError(f"未能加载协议驱动 uhf_reader.py：{_UHF_IMPORT_ERROR}")
        self._t = uhf_reader.CardReaderThread(
            port=port, baud=int(baud), adr=int(adr), msg_queue=out_queue,
            poll_interval=float(poll_interval), debounce=float(debounce_sec),
            reconnect=float(reconnect_sec), use_tid=bool(use_tid))

    def start(self):
        self._t.start()

    def stop(self):
        self._t.stop()

    @property
    def read_count(self):          # 代理到内部线程
        return self._t.read_count


def available_ports():
    """列出系统里可用的串口（设备名列表）。"""
    if uhf_reader is not None:
        return [dev for dev, _desc, _man in uhf_reader.list_serial_ports()]
    if not HAS_SERIAL:
        return []
    return [p.device for p in list_ports.comports()]


def port_details():
    """列出串口及描述，界面上显示更友好。"""
    if uhf_reader is not None:
        return uhf_reader.list_serial_ports()
    if not HAS_SERIAL:
        return []
    return [(p.device, p.description or "", p.manufacturer or "")
            for p in list_ports.comports()]


def guess_reader_port():
    """尝试自动挑一个"像读写器"的串口（CH340 / CP210x / PL2303 / FT232…）。"""
    if uhf_reader is not None:
        return uhf_reader.guess_reader_port()
    if not HAS_SERIAL:
        return ""
    keys = ("CH340", "CH341", "CP210", "PL2303", "USB-SERIAL", "USB Serial", "Silicon Labs", "FT232")
    for p in list_ports.comports():
        desc = f"{p.description} {p.manufacturer or ''}"
        if any(k.lower() in desc.lower() for k in keys):
            return p.device
    ports = available_ports()
    return ports[0] if ports else ""


# ====================================================================
# [5] 业务逻辑层 —— 签到 / 签退 到底怎么判定
# ====================================================================

class Result:
    """处理结果对象，界面层只负责把它画出来。"""
    __slots__ = ("ok", "code", "title", "message", "student", "action", "card", "ts")

    def __init__(self, ok, code, title, message, student=None, action=None,
                 card="", ts=None):
        self.ok = ok
        self.code = code          # IN / OUT / DUPLICATE / UNKNOWN / TOO_SOON / BAD_DATA
        self.title = title
        self.message = message
        self.student = student
        self.action = action
        self.card = card
        self.ts = ts or datetime.now()


class AttendanceService:
    """
    核心业务规则（评分表里那个"杀手锏"就在 decide() 里）：

      1) 卡号先做防重复：同一张卡在 debounce_seconds 秒内重复出现 -> 直接忽略。
         关键细节：命中重复时会把计时器"续上"，所以拿着卡不放手会一直忽略，
         直到卡片离开读卡器超过 3 秒，才认为是一次新的、有效的刷卡。

      2) 查卡绑定的学生，查不到 -> 陌生卡，友好提示并留痕，不崩。

      3) 判定签到还是签退：
           今天没有任何记录                -> 签到(in)
           今天最后一条是 签到(in)
                 且距现在 >= 间隔分钟数     -> 签退(out)
                 且距现在 <  间隔分钟数     -> 拒绝，提示还需等多久
           今天最后一条是 签退(out)         -> 重新签到(in)  （允许一天多轮进出）
    """

    def __init__(self, db: Database):
        self.db = db
        self.last_card = ""        # 上一次刷的卡号
        self.last_card_ts = 0.0    # 上一次刷该卡的时间戳

    def handle_card(self, raw_card, now: datetime | None = None) -> Result:
        now = now or datetime.now()

        # ---- 步骤 0：归一化 + 合法性校验 ----
        card = normalize_card(raw_card)
        if not is_valid_card(card):
            return Result(False, "BAD_DATA", "无法识别的数据",
                          f"读到的内容为「{raw_card!r}」，不是合法卡号，已忽略。",
                          card=card, ts=now)

        # ---- 步骤 1：防重复刷卡（滑动窗口去抖） ----
        debounce = self.db.get_float("debounce_seconds", 3.0)
        stamp = now.timestamp()
        if card == self.last_card and (stamp - self.last_card_ts) < debounce:
            self.last_card_ts = stamp      # 续期：卡还在读卡器上，继续忽略
            return Result(False, "DUPLICATE", "重复刷卡已忽略",
                          f"卡号 {card} 与上一次刷卡间隔小于 {debounce:.0f} 秒，"
                          f"判定为同一次刷卡，未重复录入。",
                          card=card, ts=now)

        self.last_card, self.last_card_ts = card, stamp

        # ---- 步骤 2：查学生 ----
        stu = self.db.get_student_by_card(card)
        if stu is None:
            self.db.log_unknown(card, now)
            return Result(False, "UNKNOWN", "陌生卡 / 未绑定",
                          f"卡号 {card} 尚未绑定任何学生。\n"
                          f"请到【学生管理】页录入学生并绑定这张卡，本次刷卡已记录到陌生卡日志。",
                          card=card, ts=now)

        # ---- 步骤 3：判定签到 / 签退 ----
        day = now.strftime("%Y-%m-%d")
        last = self.db.latest_record_of_day(stu["id"], day)
        interval = self.db.get_float("interval_minutes", 30.0)

        if last is None:
            action = "in"
        elif last["action"] == "in":
            delta_min = (now - parse_ts(last["ts"])).total_seconds() / 60.0
            if delta_min < interval:
                left = interval - delta_min
                return Result(False, "TOO_SOON", "间隔不足，暂不能签退",
                              f"{stu['name']} 于 {last['ts']} 签到，"
                              f"距现在仅 {delta_min:.1f} 分钟。\n"
                              f"签退需间隔满 {interval:.0f} 分钟，还需等待约 {left:.1f} 分钟。",
                              student=stu, card=card, ts=now)
            action = "out"
        else:
            action = "in"

        rid = self.db.add_record(stu, card, action, now)
        act_cn = "签到" if action == "in" else "签退"
        return Result(True, action.upper(), f"{stu['name']} {act_cn}成功",
                      f"学号 {stu['student_no']}｜{stu['class_name'] or '未填班级'}\n"
                      f"时间 {now.strftime('%Y-%m-%d %H:%M:%S')}｜记录号 #{rid}",
                      student=stu, action=action, card=card, ts=now)


# ====================================================================
# [6] 界面层
# ====================================================================

def enable_hidpi():
    """
    开启高分屏适配。
    不做这一步，Windows 会把程序当成"不支持 DPI"的老程序，
    直接拉伸位图，结果就是文字发虚、界面被放大且模糊。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # Win8.1 及以上
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()        # Win7 / Win8
    except Exception:
        pass


def beep(kind: str = "ok"):
    """蜂鸣反馈（不阻塞界面，丢到后台线程响）。"""
    if winsound is None:
        return
    freq, dur = {"ok": (1200, 100), "in": (1200, 100),
                 "out": (900, 150), "warn": (600, 200), "err": (400, 300)}.get(kind, (800, 120))
    threading.Thread(target=lambda: _safe_beep(freq, dur), daemon=True).start()


def _safe_beep(freq, dur):
    try:
        winsound.Beep(freq, dur)
    except Exception:
        pass


class AppUI:
    def __init__(self, root: "tk.Tk"):
        self.root = root
        self.db = Database()
        self.svc = AttendanceService(self.db)

        self.msg_queue: "queue.Queue" = queue.Queue()
        self.reader = None                 # SerialCardReader 实例
        self.card_buffer = ""              # 键盘模拟模式的字符缓冲
        self.card_buffer_ts = 0.0
        self.binding_sid = None            # 正在等待绑定卡号的学生 id
        self.pending_dialog = None         # 正在等待刷卡自动填卡号的弹窗
        self.last_result = None

        self._setup_window()
        self._setup_style()
        self._build_header()
        self._build_tabs()
        self._start_reader()
        self._pump()                       # 启动消息轮询
        self._tick()                       # 启动时钟
        self._refresh_all()

    # ---------------- 窗口基础 ----------------
    def _setup_window(self):
        self.root.title(f"{APP_NAME}  v{APP_VERSION}")
        # 按屏幕缩放比例换算窗口尺寸，保证高分屏上布局不被挤变形
        try:
            self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except Exception:
            self.scale = 1.0
        w, h = int(1120 * self.scale), int(760 * self.scale)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if w > sw:
            w = int(sw * 0.96)
        if h > sh:
            h = int(sh * 0.92)
        self.root.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//3)}")
        self.root.minsize(int(980 * self.scale), int(660 * self.scale))
        self.root.configure(bg=C_BG)

    def _setup_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=C_BG, foreground=C_TXT, font=("Microsoft YaHei UI", 10))
        st.configure("TFrame", background=C_BG)
        st.configure("Panel.TFrame", background=C_PANEL, relief="solid", borderwidth=1)
        st.configure("TLabel", background=C_BG, foreground=C_TXT)
        st.configure("Panel.TLabel", background=C_PANEL, foreground=C_TXT)
        st.configure("Sub.TLabel", background=C_PANEL, foreground=C_SUB,
                     font=("Microsoft YaHei UI", 9))
        st.configure("H1.TLabel", background=C_PANEL, foreground=C_TXT,
                     font=("Microsoft YaHei UI", 22, "bold"))
        st.configure("H2.TLabel", background=C_BG, foreground=C_TXT,
                     font=("Microsoft YaHei UI", 12, "bold"))
        st.configure("TNotebook", background=C_BG, borderwidth=0)
        st.configure("TNotebook.Tab", padding=(18, 9), font=("Microsoft YaHei UI", 10))
        st.map("TNotebook.Tab", background=[("selected", C_PANEL)], foreground=[("selected", C_PRIMARY)])
        st.configure("TButton", padding=(12, 6), font=("Microsoft YaHei UI", 10))
        st.configure("Primary.TButton", padding=(12, 6), foreground="#FFFFFF",
                     background=C_PRIMARY, font=("Microsoft YaHei UI", 10, "bold"))
        st.map("Primary.TButton", background=[("active", "#1E5BD0")])
        st.configure("Treeview", rowheight=26, fieldbackground=C_PANEL,
                     background=C_PANEL, font=("Microsoft YaHei UI", 9))
        st.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        st.configure("TCombobox", padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", "#FFFFFF")])

    def _entry(self, parent, var, width=26, font=None):
        """
        统一样式的输入框。
        说明：为什么不用默认样式？——tk 自带 Entry 只有 1 像素的灰边，
        放在白色面板上几乎看不见（实测截图确认过），所以这里显式指定
        白底 + 1px 实线边框 + 聚焦时变蓝的 highlight 边框。
        """
        return tk.Entry(parent, textvariable=var, width=width,
                        font=font or ("Consolas", 10),
                        bg="#FFFFFF", fg=C_TXT, insertbackground=C_TXT,
                        relief="solid", borderwidth=1,
                        highlightthickness=1, highlightbackground="#C7D0DB",
                        highlightcolor=C_PRIMARY)

    # ---------------- 顶部状态栏 ----------------
    def _build_header(self):
        bar = tk.Frame(self.root, bg="#FFFFFF", height=58)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        tk.Label(bar, text="⚡ " + APP_NAME, bg="#FFFFFF", fg=C_TXT,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(side="left", padx=16)

        self.lbl_clock = tk.Label(bar, text="", bg="#FFFFFF", fg=C_SUB,
                                  font=("Consolas", 11))
        self.lbl_clock.pack(side="right", padx=16)

        self.lbl_device = tk.Label(bar, text="设备状态：初始化…", bg="#FFFFFF",
                                   fg=C_SUB, font=("Microsoft YaHei UI", 10))
        self.lbl_device.pack(side="right", padx=16)

        tk.Frame(self.root, bg=C_BORDER, height=1).pack(fill="x", side="top")

    # ---------------- 选项卡 ----------------
    def _build_tabs(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=10)

        self.tab_scan = ttk.Frame(self.nb, padding=14)
        self.tab_hist = ttk.Frame(self.nb, padding=14)
        self.tab_stu = ttk.Frame(self.nb, padding=14)
        self.tab_set = ttk.Frame(self.nb, padding=14)
        self.tab_help = ttk.Frame(self.nb, padding=14)

        self.nb.add(self.tab_scan, text="  打卡台  ")
        self.nb.add(self.tab_hist, text="  历史记录  ")
        self.nb.add(self.tab_stu, text="  学生管理  ")
        self.nb.add(self.tab_set, text="  设置  ")
        self.nb.add(self.tab_help, text="  使用说明  ")

        self._build_scan_tab()
        self._build_hist_tab()
        self._build_stu_tab()
        self._build_set_tab()
        self._build_help_tab()

    # ---------- 打卡台 ----------
    def _build_scan_tab(self):
        f = self.tab_scan

        # 结果卡片
        self.card_frame = tk.Frame(f, bg=C_PANEL, highlightbackground=C_BORDER,
                                   highlightthickness=1)
        self.card_frame.pack(fill="x", pady=(0, 12))

        inner = tk.Frame(self.card_frame, bg=C_PANEL)
        inner.pack(fill="x", padx=24, pady=20)

        self.lbl_res_title = tk.Label(inner, text="请刷卡…", bg=C_PANEL, fg=C_SUB,
                                      font=("Microsoft YaHei UI", 24, "bold"), anchor="w")
        self.lbl_res_title.pack(anchor="w")

        self.lbl_res_msg = tk.Label(inner, text="把校园卡放到读卡器上，系统会自动识别签到 / 签退。",
                                    bg=C_PANEL, fg=C_SUB, justify="left", anchor="w",
                                    font=("Microsoft YaHei UI", 11))
        self.lbl_res_msg.pack(anchor="w", pady=(8, 0))

        self.lbl_res_card = tk.Label(inner, text="卡号：—", bg=C_PANEL, fg=C_SUB,
                                     font=("Consolas", 10), anchor="w")
        self.lbl_res_card.pack(anchor="w", pady=(8, 0))

        # 缓冲区提示 + 手动输入
        bar = tk.Frame(f, bg=C_BG)
        bar.pack(fill="x", pady=(0, 12))
        self.lbl_buf = tk.Label(bar, text="键盘捕获：等待刷卡…", bg=C_BG, fg=C_SUB,
                                font=("Consolas", 10))
        self.lbl_buf.pack(side="left")
        ttk.Button(bar, text="手动输入卡号（演示用）", command=self._manual_input)\
            .pack(side="right")

        # 今日概览
        stat = tk.Frame(f, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        stat.pack(fill="x", pady=(0, 12))
        self.stat_labels = {}
        for key, title in (("stu", "在册学生"), ("in", "今日签到"), ("out", "今日签退"),
                           ("total", "今日记录")):
            cell = tk.Frame(stat, bg=C_PANEL)
            cell.pack(side="left", expand=True, fill="both", pady=14)
            tk.Label(cell, text=title, bg=C_PANEL, fg=C_SUB,
                     font=("Microsoft YaHei UI", 9)).pack()
            lb = tk.Label(cell, text="0", bg=C_PANEL, fg=C_PRIMARY,
                          font=("Microsoft YaHei UI", 20, "bold"))
            lb.pack()
            self.stat_labels[key] = lb

        # 实时日志
        box = tk.LabelFrame(f, text=" 实时打卡日志 ", bg=C_BG, fg=C_TXT,
                            font=("Microsoft YaHei UI", 10, "bold"), padx=8, pady=8)
        box.pack(fill="both", expand=True)
        self.log_box = tk.Listbox(box, bg="#FFFFFF", fg=C_TXT, activestyle="none",
                                  font=("Consolas", 10), highlightthickness=1,
                                  highlightbackground=C_BORDER, selectbackground="#E8F0FE")
        self.log_box.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.log_box.yview)
        sb.pack(side="right", fill="y")
        self.log_box.configure(yscrollcommand=sb.set)

    # ---------- 历史记录 ----------
    def _build_hist_tab(self):
        f = self.tab_hist
        bar = tk.Frame(f, bg=C_BG)
        bar.pack(fill="x", pady=(0, 10))

        tk.Label(bar, text="日期：", bg=C_BG).pack(side="left")
        self.var_hist_day = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        self._entry(bar, self.var_hist_day, width=14).pack(side="left", ipady=2)
        self.var_hist_all = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="全部日期", variable=self.var_hist_all,
                       bg=C_BG, activebackground=C_BG, selectcolor="#FFFFFF",
                       highlightthickness=0, font=("Microsoft YaHei UI", 10)).pack(
            side="left", padx=8)

        tk.Label(bar, text="关键字：", bg=C_BG).pack(side="left", padx=(14, 0))
        self.var_hist_kw = tk.StringVar()
        self._entry(bar, self.var_hist_kw, width=18,
                    font=("Microsoft YaHei UI", 10)).pack(side="left", ipady=2)

        ttk.Button(bar, text="查询", command=self._refresh_history).pack(side="left", padx=8)
        ttk.Button(bar, text="导出 CSV", command=self._export_csv).pack(side="left")

        cols = ("id", "student_no", "student_name", "class_name", "card_no", "action", "ts")
        heads = ("记录号", "学号", "姓名", "班级", "卡号", "动作", "时间")
        widths = (70, 110, 90, 120, 130, 70, 170)
        self.tree_hist = ttk.Treeview(f, columns=cols, show="headings", height=18)
        for c, h, w in zip(cols, heads, widths):
            self.tree_hist.heading(c, text=h)
            self.tree_hist.column(c, width=w, anchor="center")
        self.tree_hist.tag_configure("in", foreground=C_IN)
        self.tree_hist.tag_configure("out", foreground=C_OUT)
        self.tree_hist.pack(fill="both", expand=True)

        self.lbl_hist_sum = tk.Label(f, text="", bg=C_BG, fg=C_SUB,
                                     font=("Microsoft YaHei UI", 9))
        self.lbl_hist_sum.pack(anchor="w", pady=(6, 0))

    # ---------- 学生管理 ----------
    def _build_stu_tab(self):
        f = self.tab_stu
        bar = tk.Frame(f, bg=C_BG)
        bar.pack(fill="x", pady=(0, 10))

        tk.Label(bar, text="搜索：", bg=C_BG).pack(side="left")
        self.var_stu_kw = tk.StringVar()
        self._entry(bar, self.var_stu_kw, width=20,
                    font=("Microsoft YaHei UI", 10)).pack(side="left", ipady=2)
        ttk.Button(bar, text="刷新", command=self._refresh_students).pack(side="left", padx=6)

        ttk.Button(bar, text="新增学生", style="Primary.TButton",
                   command=self._add_student).pack(side="right")
        ttk.Button(bar, text="编辑", command=self._edit_student).pack(side="right", padx=6)
        ttk.Button(bar, text="删除", command=self._del_student).pack(side="right")
        ttk.Button(bar, text="绑定卡号（刷卡）",
                   command=self._bind_card).pack(side="right", padx=6)
        ttk.Button(bar, text="导入示例数据",
                   command=self._seed_demo).pack(side="right")

        cols = ("id", "student_no", "name", "class_name", "card_no", "created_at")
        heads = ("ID", "学号", "姓名", "班级", "已绑定卡号", "创建时间")
        widths = (60, 120, 100, 140, 160, 170)
        self.tree_stu = ttk.Treeview(f, columns=cols, show="headings", height=16)
        for c, h, w in zip(cols, heads, widths):
            self.tree_stu.heading(c, text=h)
            self.tree_stu.column(c, width=w, anchor="center")
        self.tree_stu.pack(fill="both", expand=True)

        tk.Label(f, text="提示：先用读卡器刷你要绑定的卡，或点「绑定卡号（刷卡）」后直接刷卡自动填入。",
                 bg=C_BG, fg=C_SUB, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(6, 0))

        # 陌生卡日志
        box = tk.LabelFrame(f, text=" 陌生卡 / 未绑定卡日志 ", bg=C_BG, fg=C_TXT,
                            font=("Microsoft YaHei UI", 10, "bold"), padx=8, pady=8)
        box.pack(fill="x", pady=(10, 0))
        self.list_unknown = tk.Listbox(box, height=5, font=("Consolas", 9),
                                       bg="#FFFFFF", highlightthickness=1,
                                       highlightbackground=C_BORDER)
        self.list_unknown.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.list_unknown.yview)
        sb.pack(side="right", fill="y")
        self.list_unknown.configure(yscrollcommand=sb.set)

    # ---------- 设置 ----------
    def _build_set_tab(self):
        f = self.tab_set
        grid = tk.Frame(f, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        grid.pack(fill="x")
        g = tk.Frame(grid, bg=C_PANEL)
        g.pack(fill="x", padx=24, pady=18)

        self.var_set = {k: tk.StringVar() for k in DEFAULT_SETTINGS}

        rows = [
            ("interval_minutes", "签到签退最小间隔（分钟）",
             "核心业务规则。距离上次签到不满这个分钟数，不允许签退。"),
            ("debounce_seconds", "防重复刷卡窗口（秒）",
             "同一张卡在这个时间内重复刷到会被忽略，避免一次刷卡录入多条。"),
            ("reader_mode", "读卡器工作模式",
             "serial = 串口主动询查（KL9007U 应答模式，默认）；keyboard = 键盘模拟。"),
            ("serial_port", "串口端口",
             "KL9007U 一般为 COM9。点右侧按钮刷新/自动识别。"),
            ("serial_baud", "波特率",
             "KL9007U 默认 57600（可选 9600 / 19200 / 115200），需与读写器一致。"),
            ("reader_adr", "读写器地址",
             "出厂 0。若读不到卡，可改成 255（广播地址）再试。"),
            ("use_tid", "卡号来源",
             "0 = 用 EPC 当卡号；1 = 用 TID（唯一性更强，部分标签支持）。"),
            ("poll_interval", "询查间隔（秒）",
             "P每秒主动发询查命令的频率，越小越灵敏；0.1~0.3 比较合适。"),
            ("reconnect_seconds", "断线重连间隔（秒）",
             "读卡器被拔掉后，每隔多少秒尝试自动重连一次。"),
        ]
        for i, (key, label, tip) in enumerate(rows):
            tk.Label(g, text=label, bg=C_PANEL, fg=C_TXT, anchor="w",
                     font=("Microsoft YaHei UI", 10, "bold")).grid(
                row=i * 2, column=0, sticky="w", pady=(10, 0), padx=(0, 16))
            if key == "reader_mode":
                ttk.Combobox(g, textvariable=self.var_set[key], width=25,
                             values=["keyboard", "serial"], state="readonly")\
                    .grid(row=i * 2, column=1, sticky="w", pady=(10, 0))
            elif key == "serial_port":
                self.cmb_port = ttk.Combobox(g, textvariable=self.var_set[key], width=25)
                self.cmb_port.grid(row=i * 2, column=1, sticky="w", pady=(10, 0))
                ttk.Button(g, text="刷新串口", command=self._refresh_ports)\
                    .grid(row=i * 2, column=2, sticky="w", padx=8, pady=(10, 0))
                ttk.Button(g, text="自动识别", command=self._auto_port)\
                    .grid(row=i * 2, column=3, sticky="w", pady=(10, 0))
            else:
                self._entry(g, self.var_set[key], width=30).grid(
                    row=i * 2, column=1, sticky="w", pady=(10, 0), ipady=3)
            tk.Label(g, text=tip, bg=C_PANEL, fg=C_SUB, anchor="w",
                     font=("Microsoft YaHei UI", 9)).grid(
                row=i * 2 + 1, column=0, columnspan=4, sticky="w", pady=(2, 4))

        ttk.Button(g, text="保存设置并重启读卡器", style="Primary.TButton",
                   command=self._save_settings).grid(row=99, column=0, columnspan=2,
                                                     sticky="w", pady=(16, 0))
        ttk.Button(g, text="运行自检测试", command=self._run_selftest_ui)\
            .grid(row=99, column=2, sticky="w", pady=(16, 0))
        ttk.Button(g, text="🔍 刷卡没反应？打开读卡诊断工具", command=self._open_diagnose)\
            .grid(row=99, column=3, sticky="w", pady=(16, 0))

        self.lbl_set_msg = tk.Label(f, text="", bg=C_BG, fg=C_SUB,
                                    font=("Microsoft YaHei UI", 10))
        self.lbl_set_msg.pack(anchor="w", pady=(12, 0))

        # ---- 运行环境自检面板（答辩时可以直接展示这一块）----
        env = tk.LabelFrame(f, text=" 运行环境自检 ", bg=C_BG, fg=C_TXT,
                            font=("Microsoft YaHei UI", 10, "bold"), padx=10, pady=8)
        env.pack(fill="x", pady=(12, 0))

        pyver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        if HAS_SERIAL:
            ser_txt = f"✅ 串口库 pyserial {getattr(serial, '__version__', '?')}（来源：{SERIAL_FROM}）"
            ser_color = C_IN
        else:
            ser_txt = ("❌ 串口库 pyserial 不可用 —— 请把整个项目文件夹完整解压后再运行"
                       "（项目内置了 _vendor/serial，缺了它就会报这个错）")
            ser_color = C_ERR

        tk.Label(env, text=f"🐍 正在使用的 Python：{sys.executable}（{pyver}）",
                 bg=C_BG, fg=C_TXT, anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        tk.Label(env, text=ser_txt, bg=C_BG, fg=ser_color, anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))

        ports_now = available_ports()
        tk.Label(env, text=("🔌 当前检测到的串口：" + ("、".join(ports_now) if ports_now else "无")),
                 bg=C_BG, fg=C_TXT, anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))

        bar = tk.Frame(env, bg=C_BG)
        bar.pack(anchor="w", pady=(8, 0))
        ttk.Button(bar, text="🔄 重新检测依赖", command=self._recheck_deps).pack(side="left")
        ttk.Button(bar, text="🛠 一键安装 pyserial（联网）",
                   command=self._install_pyserial).pack(side="left", padx=8)
        tk.Label(bar, text="（断网也能用：程序已内置 pyserial，正常不会走到这一步）",
                 bg=C_BG, fg=C_SUB, font=("Microsoft YaHei UI", 8)).pack(side="left")

    # ---------- 使用说明 ----------
    def _build_help_tab(self):
        txt = tk.Text(self.tab_help, wrap="word", font=("Microsoft YaHei UI", 10),
                      bg="#FFFFFF", fg=C_TXT, relief="solid", borderwidth=1,
                      padx=16, pady=14)
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")

    # =================================================================
    # 读卡器启动 / 消息循环
    # =================================================================
    def _start_reader(self):
        """按设置启动读卡器。每次切换模式都先停掉旧的线程。"""
        if self.reader:
            self.reader.stop()
            self.reader = None

        mode = self.db.get_setting("reader_mode", "keyboard")
        if mode == "serial":
            if not HAS_SERIAL:
                self._set_device("设备状态：串口库 pyserial 不可用 —— 请到【设置】页点"
                                 "「重新检测依赖」；确认项目里 _vendor 文件夹完整", C_ERR)
                return
            port = self.db.get_setting("serial_port", "").strip() or guess_reader_port()
            if not port:
                self._set_device("设备状态：未找到可用串口，请在【设置】里指定", C_ERR)
                return
            try:
                baud = int(self.db.get_setting("serial_baud", "57600"))
            except ValueError:
                baud = 57600
            try:
                adr = int(self.db.get_setting("reader_adr", "0"))
            except ValueError:
                adr = 0
            try:
                poll = float(self.db.get_setting("poll_interval", "0.15"))
            except ValueError:
                poll = 0.15
            use_tid = self.db.get_setting("use_tid", "0") == "1"
            rec = self.db.get_float("reconnect_seconds", 3.0)
            debounce = self.db.get_float("debounce_seconds", 3.0)
            try:
                self.reader = SerialCardReader(port, baud, self.msg_queue, rec,
                                               adr=adr, use_tid=use_tid,
                                               poll_interval=poll, debounce_sec=debounce)
                self.reader.start()
                self._set_device(f"设备状态：正在连接串口 {port} @ {baud} …", C_SUB)
            except Exception as e:
                self.reader = None
                self._set_device(f"设备状态：读卡器启动失败 {e}", C_ERR)
        else:
            self._set_device("设备状态：键盘模拟模式（请把光标停在打卡台页面，直接刷卡）", C_SUB)

    def _set_device(self, text: str, color: str = C_SUB):
        self.lbl_device.configure(text=text, fg=color)

    def _manual_input(self):
        s = simpledialog.askstring("手动输入卡号", "请输入卡号（演示 / 无设备时使用）：",
                                   parent=self.root)
        if s:
            self.msg_queue.put(("raw", s))

    def _pump(self):
        """主线程轮询消息队列 —— tkinter 不是线程安全的，必须这样回传。"""
        try:
            while True:
                item = self.msg_queue.get_nowait()
                kind = item[0]
                if kind in ("raw", "card"):
                    # raw  = 键盘模拟模式 / 手动输入
                    # card = 串口协议驱动读到的 EPC
                    self._on_card(item[1])
                elif kind == "status":
                    _, text, level = item
                    color = {"ok": C_IN, "err": C_ERR, "warn": C_OUT}.get(level, C_SUB)
                    self._set_device("设备状态：" + text, color)
        except queue.Empty:
            pass

        # 键盘模式：无结束符超时成帧
        if self.card_buffer and (time.time() - self.card_buffer_ts) > 0.5:
            buf, self.card_buffer = self.card_buffer, ""
            self.msg_queue.put(("raw", buf))

        self.root.after(80, self._pump)

    def _tick(self):
        self.lbl_clock.configure(text=datetime.now().strftime("%Y-%m-%d %H:%M:%S  %A"))
        self.root.after(500, self._tick)

    # ---------------- 全局键盘捕获（键盘模拟模式的核心） ----------------
    def on_key(self, event):
        # 平时只在「打卡台」页生效；但在等待绑定卡号时，任何页面都要能刷卡
        capture = False
        try:
            capture = (self.nb.index(self.nb.select()) == 0)
        except Exception:
            capture = True
        if self.binding_sid is not None or self.pending_dialog is not None:
            capture = True
        if not capture:
            return
        # 焦点在输入框里时让输入框正常工作
        try:
            fw = self.root.focus_get()
            if isinstance(fw, (tk.Entry, tk.Text, tk.Listbox)):
                return
        except Exception:
            pass

        if event.keysym == "Return":
            if self.card_buffer:
                buf, self.card_buffer = self.card_buffer, ""
                self.msg_queue.put(("raw", buf))
            return
        if event.char and len(event.char) == 1 and event.char.isprintable():
            self.card_buffer += event.char
            self.card_buffer_ts = time.time()
            self.lbl_buf.configure(text="键盘捕获：" + self.card_buffer, fg=C_PRIMARY)

    # =================================================================
    # 刷卡处理
    # =================================================================
    def _on_card(self, raw):
        # 情形 A：某个弹窗正在等待刷卡自动填入卡号
        dlg = self.pending_dialog
        if dlg is not None:
            try:
                if dlg.winfo_exists():
                    card = normalize_card(raw)
                    if is_valid_card(card):
                        dlg.v_card.set(card)
                        self.pending_dialog = None
                        dlg.lift()
                        return
                else:
                    self.pending_dialog = None
            except Exception:
                self.pending_dialog = None

        # 情形 B：正在等待把某张卡绑定给指定学生
        if self.binding_sid is not None:
            card = normalize_card(raw)
            if is_valid_card(card):
                sid = self.binding_sid
                self.binding_sid = None
                try:
                    stu = self.db.conn.execute(
                        "SELECT * FROM students WHERE id=?", (sid,)).fetchone()
                    self.db.update_student(sid, stu["student_no"], stu["name"],
                                           stu["class_name"], card)
                    self._refresh_students()
                    self._info(f"已将卡号 {card} 绑定到 {stu['name']}")
                except sqlite3.IntegrityError:
                    self._warn(f"绑定失败：卡号 {card} 已被其它学生绑定")
                return

        res = self.svc.handle_card(raw)
        self.last_result = res
        self._render_result(res)
        self._append_log(res)
        self._refresh_stats()
        if self.db.get_setting("reader_mode") == "serial":
            self._refresh_history_silent()

        kind = {"IN": "in", "OUT": "out", "DUPLICATE": "warn",
                "UNKNOWN": "warn", "TOO_SOON": "warn", "BAD_DATA": "err"}.get(res.code, "ok")
        beep(kind)

    def _render_result(self, res: Result):
        color = {"IN": C_IN, "OUT": C_OUT}.get(res.code, C_ERR)
        if res.code == "DUPLICATE":
            color = C_SUB
        self.card_frame.configure(highlightbackground=color, highlightthickness=2)
        self.lbl_res_title.configure(text=res.title, fg=color)
        self.lbl_res_msg.configure(text=res.message, fg=C_TXT)
        self.lbl_res_card.configure(
            text=f"卡号：{res.card or '—'}      {res.ts.strftime('%H:%M:%S')}", fg=C_SUB)
        self.lbl_buf.configure(text="键盘捕获：等待刷卡…", fg=C_SUB)
        self.card_buffer = ""

    def _append_log(self, res: Result):
        t = res.ts.strftime("%H:%M:%S")
        tag = "OK " if res.ok else "!! "
        name = res.student["name"] if res.student else "—"
        line = f"[{t}] {tag}{res.title:<14} {name:<8} {res.card:<14} {res.code}"
        self.log_box.insert(0, line)
        if self.log_box.size() > 300:
            self.log_box.delete(200, "end")

    # =================================================================
    # 各页刷新
    # =================================================================
    def _refresh_all(self):
        self._refresh_settings_widgets()
        self._refresh_students()
        self._refresh_history()
        self._refresh_stats()
        self._refresh_unknown()

    def _refresh_stats(self):
        day = datetime.now().strftime("%Y-%m-%d")
        s = self.db.today_stats(day)
        self.stat_labels["stu"].configure(text=str(self.db.count_students()))
        self.stat_labels["in"].configure(text=str(s["in"]), fg=C_IN)
        self.stat_labels["out"].configure(text=str(s["out"]), fg=C_OUT)
        self.stat_labels["total"].configure(text=str(s["total"]))

    def _refresh_students(self):
        kw = self.var_stu_kw.get().strip()
        for i in self.tree_stu.get_children():
            self.tree_stu.delete(i)
        for r in self.db.list_students(kw):
            self.tree_stu.insert("", "end", iid=str(r["id"]),
                                 values=(r["id"], r["student_no"], r["name"],
                                         r["class_name"] or "", r["card_no"] or "—",
                                         r["created_at"]))
        self._refresh_unknown()
        self._refresh_stats()

    def _refresh_unknown(self):
        self.list_unknown.delete(0, "end")
        for r in self.db.list_unknown(50):
            self.list_unknown.insert("end", f"卡号 {r['card_no']:<18}  {r['ts']}")

    def _refresh_history(self):
        day = "" if self.var_hist_all.get() else self.var_hist_day.get().strip()
        kw = self.var_hist_kw.get().strip()
        rows = self.db.query_records(day, kw)
        for i in self.tree_hist.get_children():
            self.tree_hist.delete(i)
        cnt_in = cnt_out = 0
        for r in rows:
            act = "签到" if r["action"] == "in" else "签退"
            if r["action"] == "in":
                cnt_in += 1
            else:
                cnt_out += 1
            self.tree_hist.insert("", "end", values=(
                r["id"], r["student_no"], r["student_name"], r["class_name"] or "",
                r["card_no"], act, r["ts"]), tags=(r["action"],))
        scope = day if day else "全部日期"
        self.lbl_hist_sum.configure(
            text=f"{scope}：共 {len(rows)} 条记录（签到 {cnt_in} 次 / 签退 {cnt_out} 次）")

    def _refresh_history_silent(self):
        try:
            self._refresh_history()
        except Exception:
            pass

    def _refresh_settings_widgets(self):
        for k in DEFAULT_SETTINGS:
            self.var_set[k].set(self.db.get_setting(k, DEFAULT_SETTINGS[k]))
        self._refresh_ports()

    def _refresh_ports(self):
        """刷新串口下拉框，并给出反馈（不再"点了没反应"）。"""
        if not HAS_SERIAL:
            self._warn("串口库不可用，无法枚举串口。\n\n"
                       "请确认：① 项目文件夹是完整解压的（里面应有 _vendor 目录）；\n"
                       "② 点【重新检测依赖】；③ 仍不行就点【一键安装 pyserial】。")
            return
        ports = available_ports()
        try:
            self.cmb_port.configure(values=ports)
        except Exception:
            pass
        if ports:
            self._info("检测到串口：" + "、".join(ports) +
                       f"\n自动识别建议：{guess_reader_port() or '无'}")
        else:
            self._warn("没有检测到任何串口。请确认读写器已插好、USB 转串口驱动已安装"
                       "（设备管理器里应能看到 COM 端口）。")

    def _recheck_deps(self):
        """重新加载串口库（补齐 _vendor 或刚装好 pyserial 后用）。"""
        if reload_serial():
            self._info(f"串口库已就绪（来源：{SERIAL_FROM}）。\n"
                       f"请到【设置】页点「保存设置并重启读卡器」以启用串口读卡。")
            self._refresh_ports()
        else:
            self._warn("仍然没有找到 pyserial。\n"
                       "请检查项目目录下是否有 _vendor\\serial 文件夹；\n"
                       "或点【一键安装 pyserial（联网）】自动安装。")

    def _install_pyserial(self):
        """联网给"当前正在运行的这个 Python"安装 pyserial。"""
        if not messagebox.askyesno("安装 pyserial",
                                   "将执行：\n"
                                   f"{sys.executable} -m pip install pyserial\n\n继续吗？"):
            return
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "pyserial"],
                               capture_output=True, text=True, timeout=180)
            tail = (r.stdout or "")[-600:] + (r.stderr or "")[-300:]
            if reload_serial():
                messagebox.showinfo("安装成功", "pyserial 安装完成，已可正常使用。\n\n"
                                                "请到【设置】页点「保存设置并重启读卡器」。")
            else:
                messagebox.showwarning("安装未成功",
                                       "pip 返回信息如下（可发给老师/同学排查）：\n\n" + tail)
        except Exception as e:
            messagebox.showerror("安装失败", f"执行 pip 时出错：{e}\n\n"
                                             "提示：本项目已内置 pyserial，"
                                             "通常不需要联网安装。")

    def _auto_port(self):
        p = guess_reader_port()
        if p:
            self.var_set["serial_port"].set(p)
            self._info(f"已自动识别到可能的读卡器端口：{p}")
        else:
            self._warn("没有自动识别到串口设备，请手动选择。")

    # =================================================================
    # 学生管理操作
    # =================================================================
    def _selected_student_id(self):
        sel = self.tree_stu.selection()
        if not sel:
            self._warn("请先在列表里选中一名学生。")
            return None
        return int(sel[0])

    def _add_student(self):
        StudentDialog(self.root, self.db, on_save=self._refresh_students)

    def _edit_student(self):
        sid = self._selected_student_id()
        if sid is None:
            return
        stu = self.db.conn.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
        StudentDialog(self.root, self.db, on_save=self._refresh_students, student=stu)

    def _del_student(self):
        sid = self._selected_student_id()
        if sid is None:
            return
        stu = self.db.conn.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
        if messagebox.askyesno("确认删除",
                               f"确定删除学生「{stu['name']}（{stu['student_no']}）」吗？\n"
                               f"历史打卡记录会保留。", parent=self.root):
            self.db.delete_student(sid)
            self._refresh_students()

    def _bind_card(self):
        sid = self._selected_student_id()
        if sid is None:
            return
        stu = self.db.conn.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
        self.binding_sid = sid
        self._info(f"请在读卡器上刷要绑定给「{stu['name']}」的卡…（点确定后回到打卡台即可刷卡）")

    def _seed_demo(self):
        # 示例卡号用 KL9007U 读出来的 EPC 风格（十六进制串）
        demo = [("2023001", "张三", "计算机2301", "001158230000"),
                ("2023002", "李四", "计算机2301", "013902000083EA4C"),
                ("2023003", "王五", "计算机2302", "E2004000840F020012909375"),
                ("2023004", "赵六", "计算机2302", "00112233445566778899")]
        added = 0
        for no, name, cls, card in demo:
            try:
                self.db.add_student(no, name, cls, card)
                added += 1
            except sqlite3.IntegrityError:
                pass
        self._refresh_students()
        self._info(f"已导入 {added} 条示例学生数据。\n"
                   f"示例卡号(EPC)：001158230000 / 013902000083EA4C / …\n\n"
                   f"提示：真实使用时，请刷卡自动读取，不要手抄卡号。")

    # =================================================================
    # 历史记录操作
    # =================================================================
    def _export_csv(self):
        os.makedirs(EXPORT_DIR, exist_ok=True)
        default = f"打卡记录_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path = filedialog.asksaveasfilename(
            parent=self.root, title="导出打卡记录", initialdir=EXPORT_DIR,
            initialfile=default, defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv")])
        if not path:
            return
        day = "" if self.var_hist_all.get() else self.var_hist_day.get().strip()
        rows = self.db.query_records(day, self.var_hist_kw.get().strip(), limit=100000)
        # utf-8-sig：带 BOM，Excel 打开不乱码
        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.writer(fp)
            w.writerow(["记录号", "学号", "姓名", "班级", "卡号", "动作", "打卡时间"])
            for r in rows:
                w.writerow([r["id"], r["student_no"], r["student_name"],
                            r["class_name"] or "", r["card_no"],
                            "签到" if r["action"] == "in" else "签退", r["ts"]])
        self._info(f"已导出 {len(rows)} 条记录到：\n{path}")

    # =================================================================
    # 设置操作
    # =================================================================
    def _save_settings(self):
        for k in DEFAULT_SETTINGS:
            self.db.set_setting(k, self.var_set[k].get().strip())
        self._start_reader()
        self.lbl_set_msg.configure(text="设置已保存，读卡器已按新配置重启。", fg=C_IN)
        self._info("设置已保存。")

    def _run_selftest_ui(self):
        ok, total, report = run_selftest(verbose=False)
        self.lbl_set_msg.configure(text=f"自检测试：{ok}/{total} 通过", fg=C_IN if ok == total else C_ERR)
        messagebox.showinfo("自检测试结果", report, parent=self.root)

    def _open_diagnose(self):
        """打开独立的读卡诊断工具，用来查"刷卡没反应"的原因。"""
        diag = os.path.join(BASE_DIR, "读卡诊断.py")
        if not os.path.exists(diag):
            messagebox.showwarning(
                "找不到诊断工具",
                f"没找到 {diag}\n请确认 读卡诊断.py 和 attendance.py 放在同一个文件夹里。",
                parent=self.root)
            return
        try:
            subprocess.Popen([sys.executable, diag],
                             cwd=BASE_DIR,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            self.lbl_set_msg.configure(
                text="诊断工具已在新窗口打开：把卡放到读写器上，点「自动探测波特率」",
                fg=C_IN)
        except Exception as e:
            messagebox.showerror("启动失败", f"无法启动诊断工具：{e}\n"
                                             f"也可以手动运行： python 读卡诊断.py",
                                 parent=self.root)

    # =================================================================
    # 小工具
    # =================================================================
    def _info(self, msg):
        messagebox.showinfo("提示", msg, parent=self.root)

    def _warn(self, msg):
        messagebox.showwarning("注意", msg, parent=self.root)

    def on_close(self):
        try:
            if self.reader:
                self.reader.stop()
        except Exception:
            pass
        self.db.close()
        self.root.destroy()


class StudentDialog(tk.Toplevel):
    """新增 / 编辑学生信息。卡号可以直接刷卡自动填入。"""

    def __init__(self, master, db: Database, on_save=None, student=None):
        super().__init__(master)
        self.db = db
        self.on_save = on_save
        self.student = student
        self.title("编辑学生" if student else "新增学生")
        self.configure(bg=C_PANEL)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        pad = {"padx": 14, "pady": 6}
        self.v_no = tk.StringVar(value=student["student_no"] if student else "")
        self.v_name = tk.StringVar(value=student["name"] if student else "")
        self.v_cls = tk.StringVar(value=(student["class_name"] or "") if student else "")
        self.v_card = tk.StringVar(value=(student["card_no"] or "") if student else "")

        rows = [("学号 *", self.v_no), ("姓名 *", self.v_name),
                ("班级", self.v_cls), ("卡号", self.v_card)]
        for i, (label, var) in enumerate(rows):
            tk.Label(self, text=label, bg=C_PANEL, anchor="w",
                     font=("Microsoft YaHei UI", 10)).grid(
                row=i, column=0, sticky="w", **pad)
            tk.Entry(self, textvariable=var, width=26, font=("Consolas", 10),
                     bg="#FFFFFF", fg=C_TXT, insertbackground=C_TXT,
                     relief="solid", borderwidth=1, highlightthickness=1,
                     highlightbackground="#C7D0DB", highlightcolor=C_PRIMARY).grid(
                row=i, column=1, sticky="w", pady=6, ipady=3)

        ttk.Button(self, text="读取当前刷到的卡号",
                   command=self._grab_card).grid(row=4, column=1, sticky="w", padx=14)
        ttk.Button(self, text="保存", style="Primary.TButton",
                   command=self._save).grid(row=5, column=1, sticky="e", padx=14, pady=(12, 16))
        ttk.Button(self, text="取消",
                   command=self.destroy).grid(row=5, column=0, sticky="w", padx=14, pady=(12, 16))

        self._center(master)

    def _center(self, master):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 3
        self.geometry(f"+{x}+{y}")

    def _grab_card(self):
        # 告诉主界面："下一次刷卡请填到这个弹窗里"
        self.master.pending_dialog = self
        messagebox.showinfo("请刷卡",
                            "请把卡放到读卡器上，读到的卡号会自动填入。\n"
                            "（如果是键盘模拟模式，也可以直接把卡号手打进去）",
                            parent=self)

    def _save(self):
        no, name = self.v_no.get().strip(), self.v_name.get().strip()
        if not no or not name:
            messagebox.showwarning("信息不完整", "学号和姓名必须填写。", parent=self)
            return
        card = normalize_card(self.v_card.get())
        try:
            if self.student:
                self.db.update_student(self.student["id"], no, name, self.v_cls.get(), card)
            else:
                self.db.add_student(no, name, self.v_cls.get(), card)
        except sqlite3.IntegrityError as e:
            messagebox.showerror("保存失败",
                                 f"学号或卡号重复了：\n{e}\n\n提示：一个学号/一张卡只能对应一个学生。",
                                 parent=self)
            return
        if self.on_save:
            self.on_save()
        self.destroy()


# ====================================================================
# [7] 自检测试 —— 覆盖评分表要求的正常 + 异常用例
#     运行： python attendance.py --selftest
# ====================================================================

def run_selftest(verbose: bool = True):
    import tempfile

    db_path = os.path.join(tempfile.gettempdir(), "attendance_selftest.db")
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass

    db = Database(db_path)
    svc = AttendanceService(db)
    db.add_student("2023001", "张三", "计算机2301", "E04A1B2C")

    cases = []          # (编号, 说明, 期望, 是否通过)

    def check(no, desc, expect, actual):
        cases.append((no, desc, expect, actual, expect == actual))

    # --- TC01 卡号归一化：带帧头帧尾 / 空格 / 小写 ---
    check("TC01", "脏数据归一化 b'\\x02 e0 4a-1b 2c \\r\\n' -> E04A1B2C",
          "E04A1B2C", normalize_card(b"\x02 e0 4a-1b 2c \r\n"))
    check("TC02", "非法短数据被判定为脏数据", False, is_valid_card("AB"))
    check("TC03", "合法卡号通过校验", True, is_valid_card("E04A1B2C"))

    # --- TC04 正常签到 ---
    t0 = datetime(2026, 10, 15, 8, 0, 0)
    r = svc.handle_card("E04A1B2C", now=t0)
    check("TC04", "首次刷卡 -> 签到", "IN", r.code)

    # --- TC05 重复刷卡（1 秒内）---
    r = svc.handle_card("E04A1B2C", now=t0.replace(second=1))
    check("TC05", "1 秒内重复刷同一张卡 -> 忽略", "DUPLICATE", r.code)

    # --- TC06 间隔不足不能签退 ---
    r = svc.handle_card("E04A1B2C", now=t0.replace(minute=10))
    check("TC06", "间隔 10 分钟刷同一张卡 -> 拒绝签退", "TOO_SOON", r.code)

    # --- TC07 间隔满足后签退 ---
    r = svc.handle_card("E04A1B2C", now=t0.replace(minute=31))
    check("TC07", "间隔 31 分钟 -> 签退", "OUT", r.code)

    # --- TC08 签退后可再次签到 ---
    r = svc.handle_card("E04A1B2C", now=t0.replace(hour=13))
    check("TC08", "签退之后再次刷卡 -> 重新签到", "IN", r.code)

    # --- TC09 陌生卡友好处理 ---
    r = svc.handle_card("DEADBEEF", now=t0.replace(hour=14))
    check("TC09", "未绑定卡 -> 提示陌生卡而不是崩溃", "UNKNOWN", r.code)
    check("TC10", "陌生卡写入日志表", 1, len(db.list_unknown()))

    # --- TC11 完全乱码数据 ---
    r = svc.handle_card("请输入卡号", now=t0.replace(hour=15))
    check("TC11", "非卡号文本 -> 判定为无效数据", "BAD_DATA", r.code)

    # --- TC12 数据库落库与按天查询 ---
    recs = db.query_records("2026-10-15")
    check("TC12", "按日期查询到 3 条有效记录（重复/拒绝的刷卡不入库）", 3, len(recs))
    check("TC13", "记录里保留学号姓名的快照", "张三", recs[0]["student_name"])

    # --- TC14 未知串口不会让程序崩溃 ---
    try:
        rd = SerialCardReader("COM_NOT_EXIST", 9600, queue.Queue(), 0.1)
        rd.start(); time.sleep(0.4); rd.stop()
        check("TC14", "串口不存在时线程不崩溃、可安全停止", True, True)
    except Exception as e:
        check("TC14", f"串口异常未处理好：{e}", True, False)

    db.close()

    ok = sum(1 for c in cases if c[4])
    total = len(cases)
    report_lines = []
    for no, desc, exp, act, passed in cases:
        report_lines.append(f"[{'PASS' if passed else 'FAIL'}] {no} {desc}")
        if not passed:
            report_lines.append(f"        期望 {exp!r}，实际 {act!r}")
    summary = f"自检结果：{ok}/{total} 通过\n\n" + "\n".join(report_lines)
    if verbose:
        print(summary)
    return ok, total, summary


HELP_TEXT = """【一、怎么用这套系统】

1. 启动：双击「启动程序.bat」，或在命令行执行 python attendance.py
2. 先到【学生管理】页录入学生，并把卡号绑上去（可以刷卡自动填）。
   没有读卡器时，点「导入示例数据」会用内置的示例卡号建 4 条数据，
   然后在【打卡台】页点「手动输入卡号」也能演示。
3. 回到【打卡台】页，把卡放到读卡器上：
      · 今天第一次刷          -> 签到
      · 距离签到满 30 分钟再刷 -> 签退
      · 30 分钟以内再刷        -> 会被拒绝并提示还差多久
4. 【历史记录】页可以按日期/关键字查询，并导出 CSV（Excel 可直接打开）。


【二、读卡器怎么连】（KL9007U / UHFReader18，应答模式）

本读卡器是超高频读写器，默认工作在【应答模式】：它不会主动吐卡号，
必须由程序按协议发"询查"命令。所以：
 1) 装好 USB 转串口驱动（开发包里带 CP210x / CH340 驱动）；本机是 CH340；
 2) 设备管理器里确认它占用的串口，一般是 COM9；
 3) 波特率按读写器设置填，KL9007U 默认 57600；
 4) 串口库 pyserial 已内置在项目 _vendor 目录，【无需 pip install】；
 5) 【设置】页确认 工作模式=serial、端口=COM9、波特率=57600，保存。
    （打开【设置】页能看到"运行环境自检"：当前用的哪个 Python、串口库从哪来、
      以及系统里检测到哪些串口，答辩时可直接展示。）

 ⚠ 注意：读写器同一时刻只能被一个程序占用。如果厂商演示软件
    （UHFReader18CSharp）还开着，请先关掉，否则本程序打不开串口。

 刷卡前想先确认硬件是否正常，强烈建议先跑一次命令行自测：
     python read_test.py
 它会打印读写器信息，并把读到的卡号(EPC) 实时打出来。


【三、异常情况是怎么处理的】（答辩会被问）

 · 陌生空白卡      -> 界面友好提示，同时把卡号写进「陌生卡日志」，程序不退出。
 · 连续重复刷卡    -> 同一张卡在"防重复窗口"内重复读到一律忽略；拿着卡不放手
                      会一直忽略，直到卡片离开读卡器超过该窗口，才算新的一次刷卡。
 · 读卡器被拔掉    -> 串口读写抛异常 -> 捕获后提示"读卡器异常"，
                      每隔几秒自动重连一次，界面照常能用，不会闪退。
 · 读到乱码/半截帧 -> 帧长 + CRC 双重校验，非法帧直接丢弃；卡号再做一次合法性校验。


【四、想改规则去哪改】

签到签退的 30 分钟间隔不需要改代码：
【设置】页 ->「签到签退最小间隔（分钟）」改成任意值，保存即可。
（这个值存在数据库的 settings 表里，代码里通过 db.get_setting 读取。）


【五、自检】

【设置】页点「运行自检测试」，或在命令行执行： python attendance.py --selftest
会跑 14 条正常 + 异常测试用例（含脏数据、重复刷卡、间隔不足、陌生卡、串口异常）。
协议层（组帧/CRC/解析）的单元自检： python uhf_reader.py


【六、关于 pyserial（必看，遇到过"明明装了却提示没装"）】

本程序把 pyserial 直接放在了项目自带的 _vendor 文件夹里（内置依赖），
程序启动时会自动加载它，所以：
    不需要 pip install，不需要联网，用哪个 Python 打开都能工作。

如果哪天【设置】页仍提示 pyserial 不可用，按顺序检查：
 1) 项目文件夹是不是完整解压的？必须能看到 _vendor\\serial 这个目录。
    （从压缩包里"直接打开"运行、或只复制了 py 文件，都会缺这个目录。）
 2) 点【设置】页的「🔄 重新检测依赖」。
 3) 还不行就点「🛠 一键安装 pyserial（联网）」，它会针对当前正在运行的
    这个 Python 执行 pip install pyserial。

 ⚠ 提醒：一台电脑上常常装了多个 Python（我这台就有 3.10 / 3.12 / 3.13 / 3.14）。
    pip install pyserial 只装进"某一个"，而程序可能被"另一个" Python 启动，
   就会出现"我明明装了却报没装"。【设置】页会显示当前实际使用的 Python 路径，
   答辩时可以直接指着它解释这个问题。
"""


# ====================================================================
# main
# ====================================================================

def main():
    argv = sys.argv[1:]

    if "--selftest" in argv:
        ok, total, _ = run_selftest(verbose=True)
        sys.exit(0 if ok == total else 1)

    if "--list-ports" in argv:
        print(f"运行环境：{sys.executable}  (Python "
              f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro})")
        print(f"串口库来源：{SERIAL_FROM or '未找到'}")
        if not HAS_SERIAL:
            print("串口库不可用。请确认项目里 _vendor\\serial 目录存在（完整解压），"
                  "或在【设置】页点「一键安装 pyserial」。")
            sys.exit(1)
        details = port_details()
        if not details:
            print("没有检测到可用串口。请确认读写器已插好、驱动已装。")
        else:
            print("检测到以下串口：")
            for dev, desc, man in details:
                print(f"  {dev:<10} {desc}  [{man or '-'}]")
            print(f"\n自动识别建议使用： {guess_reader_port() or '无'}")
        sys.exit(0)

    if not HAS_TK:
        print("当前 Python 没有 tkinter，无法启动图形界面。")
        print("你可以先用命令行的自检测试试逻辑： python attendance.py --selftest")
        sys.exit(1)

    enable_hidpi()          # 必须在创建窗口之前调用
    root = tk.Tk()
    app = AppUI(root)
    root.bind("<Key>", app.on_key)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
