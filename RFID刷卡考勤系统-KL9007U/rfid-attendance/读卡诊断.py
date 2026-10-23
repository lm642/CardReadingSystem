# -*- coding: utf-8 -*-
"""
====================================================================
 读卡诊断.py —— KL9007U / UHFReader18 读写器 图形化诊断工具
====================================================================
 用途：当主程序"刷卡没反应"时，用这个工具一步步定位问题：
   第 1 步  确认串口（插拔读写器，看多出来的是哪个 COM）
   第 2 步  "读取读写器信息" —— 相当于握手，通了说明串口/波特率/地址都对
   第 3 步  "开始询查" —— 把卡放天线上，看能不能读到 EPC，并显示原始收发字节

 用法： python 读卡诊断.py
====================================================================
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import uhf_reader
from uhf_reader import UHFReader18, UHFReaderError

BAUDS = [57600, 9600, 19200, 38400, 115200]
ADDRS = [0x00, 0xFF]


class DiagApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q = queue.Queue()
        self.worker = None
        self.stop_evt = threading.Event()
        self.reader = None

        root.title("KL9007U 读卡诊断工具")
        root.geometry("880x680")
        root.minsize(820, 600)
        root.configure(bg="#F5F7FA")
        self._build()
        self._refresh_ports()
        root.after(100, self._drain)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 界面 ----------------
    def _build(self):
        f = ("Microsoft YaHei UI", 10)
        fb = ("Microsoft YaHei UI", 10, "bold")

        top = tk.Frame(self.root, bg="#FFFFFF", highlightthickness=1,
                       highlightbackground="#E5E7EB")
        top.pack(fill="x", padx=14, pady=(14, 8))
        tk.Label(top, text="KL9007U / UHFReader18 读卡诊断（应答模式）", font=fb,
                 bg="#FFFFFF", fg="#111827").pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(top,
                 text="读写器是「应答模式」：程序主动发询查命令，读写器才回卡号。\n"
                      "本工具会显示每一步收发的原始字节，方便定位是串口、波特率、地址还是协议的问题。",
                 font=("Microsoft YaHei UI", 9), bg="#FFFFFF", fg="#4B5563",
                 justify="left").pack(anchor="w", padx=14, pady=(0, 12))

        mid = tk.Frame(self.root, bg="#FFFFFF", highlightthickness=1,
                       highlightbackground="#E5E7EB")
        mid.pack(fill="x", padx=14, pady=8)

        row = tk.Frame(mid, bg="#FFFFFF")
        row.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(row, text="串口：", font=f, bg="#FFFFFF", fg="#374151").pack(side="left")
        self.var_port = tk.StringVar()
        self.cmb = ttk.Combobox(row, textvariable=self.var_port, width=40,
                                state="readonly", font=f)
        self.cmb.pack(side="left", padx=6)
        ttk.Button(row, text="刷新", command=self._refresh_ports).pack(side="left")

        tk.Label(row, text="  波特率：", font=f, bg="#FFFFFF", fg="#374151").pack(side="left")
        self.var_baud = tk.StringVar(value="57600")
        ttk.Combobox(row, textvariable=self.var_baud, width=9, state="readonly", font=f,
                     values=[str(b) for b in BAUDS]).pack(side="left", padx=4)
        tk.Label(row, text="  地址：", font=f, bg="#FFFFFF", fg="#374151").pack(side="left")
        self.var_adr = tk.StringVar(value="0")
        ttk.Combobox(row, textvariable=self.var_adr, width=6, state="readonly", font=f,
                     values=["0", "255"]).pack(side="left", padx=4)

        row2 = tk.Frame(mid, bg="#FFFFFF")
        row2.pack(fill="x", padx=14, pady=(2, 12))
        ttk.Button(row2, text="① 读取读写器信息（握手）",
                   command=self._cmd_info).pack(side="left")
        self.btn_scan = ttk.Button(row2, text="② 开始询查（把卡放天线上）",
                                   command=self._start_scan)
        self.btn_scan.pack(side="left", padx=8)
        ttk.Button(row2, text="■ 停止", command=self._stop).pack(side="left", padx=4)
        ttk.Button(row2, text="⚡ 自动探测", command=self._auto_probe).pack(side="left", padx=8)

        self.var_result = tk.StringVar(value="等待操作…")
        bar = tk.Frame(mid, bg="#F3F4F6")
        bar.pack(fill="x", padx=14, pady=(0, 12))
        tk.Label(bar, textvariable=self.var_result, font=fb, bg="#F3F4F6",
                 fg="#1D4ED8", wraplength=800, justify="left").pack(anchor="w", padx=10, pady=8)

        logf = tk.Frame(self.root, bg="#FFFFFF", highlightthickness=1,
                        highlightbackground="#E5E7EB")
        logf.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        head = tk.Frame(logf, bg="#FFFFFF")
        head.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(head, text="实时日志（原始字节 + 解析结果）", font=fb, bg="#FFFFFF",
                 fg="#111827").pack(side="left")
        ttk.Button(head, text="清空", command=self._clear).pack(side="right")
        self.txt = tk.Text(logf, font=("Consolas", 10), bg="#0F172A", fg="#E5E7EB",
                           relief="flat", insertbackground="#E5E7EB", wrap="word")
        self.txt.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        for tag, color in (("tx", "#FBBF24"), ("rx", "#34D399"), ("info", "#93C5FD"),
                           ("err", "#FCA5A5"), ("ok", "#34D399"), ("warn", "#FBBF24")):
            self.txt.tag_config(tag, foreground=color)
        self.txt.configure(state="disabled")

    # ---------------- 基础 ----------------
    def _log(self, tag, msg):
        self.txt.configure(state="normal")
        self.txt.insert("end", msg + "\n", tag)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _clear(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _refresh_ports(self):
        ports = uhf_reader.list_serial_ports()
        vals = []
        prefer = uhf_reader.guess_reader_port()
        for dev, desc, man in ports:
            mark = "  ← 可能是读写器" if dev == prefer else ""
            vals.append(f"{dev}   {desc}{mark}")
        self.cmb["values"] = vals
        if prefer:
            for v in vals:
                if v.startswith(prefer):
                    self.var_port.set(v)
                    break
        elif vals:
            self.var_port.set(vals[0])
        else:
            self.var_port.set("")
            self._log("err", "没有检测到任何串口：确认读写器插好、USB 转串口驱动已安装。")

    def _port(self):
        v = self.var_port.get().strip()
        return v.split()[0] if v else ""

    def _open(self):
        port = self._port()
        if not port:
            messagebox.showwarning("提示", "请先选择一个串口")
            return None
        r = UHFReader18(port, int(self.var_baud.get()), int(self.var_adr.get()),
                        on_log=lambda t: self.q.put(("log", ("tx" if t.startswith("[发送]") else
                                                             "rx" if t.startswith("[接收]") else "info", t))))
        try:
            r.open()
        except UHFReaderError as e:
            self._log("err", str(e))
            return None
        return r

    # ---------------- 命令 ① ----------------
    def _cmd_info(self):
        self._stop()
        r = self._open()
        if not r:
            return
        self._log("info", "→ 发送【读取读写器信息】0x21 …")
        try:
            info = r.get_reader_info()
        except UHFReaderError as e:
            self._log("err", f"通信异常：{e}")
            r.close()
            return
        if info is None:
            self.var_result.set("✘ 读写器无应答。排查：① 波特率(试 9600/57600/115200)；"
                                "② 地址(试 255)；③ 串口是否被演示软件占用；④ 读写器是否上电。")
            self._log("warn", "未收到应答。")
        else:
            self.var_result.set("✔ 握手成功，读写器在线！"
                                f"类型={info.get('type')} 版本={info.get('version')} "
                                f"功率={info.get('power')} 频段={info.get('band')}")
            self._log("ok", f"✔ 读卡器信息：{info}")
        r.close()

    # ---------------- 命令 ② ----------------
    def _start_scan(self):
        self._stop()
        port = self._port()
        if not port:
            messagebox.showwarning("提示", "请先选择一个串口")
            return
        baud, adr = int(self.var_baud.get()), int(self.var_adr.get())
        self.stop_evt = threading.Event()
        self.worker = threading.Thread(target=self._scan_loop,
                                       args=(port, baud, adr), daemon=True)
        self.worker.start()
        self.var_result.set(f"正在询查 {port}@{baud}（地址 {adr}）…… 把卡放到天线上")
        self._log("info", f"开始循环询查 {port}@{baud} 地址{adr}")

    def _scan_loop(self, port, baud, adr):
        try:
            r = UHFReader18(port, baud, adr,
                            on_log=lambda t: self.q.put(("log", ("tx" if t.startswith("[发送]") else
                                                                 "rx" if t.startswith("[接收]") else "info", t))))
            r.open()
        except Exception as e:
            self.q.put(("result", f"✘ 打开串口失败：{e}"))
            return
        info = r.get_reader_info()
        if info is None:
            self.q.put(("log", ("warn", "握手无应答，但仍继续尝试询查……")))
        seen = {}
        try:
            while not self.stop_evt.is_set():
                tags = r.inventory()
                if tags is None:
                    self.q.put(("log", ("warn", "本帧询查无应答（连接可能不稳）")))
                for epc in tags or []:
                    n = seen.get(epc, 0) + 1
                    seen[epc] = n
                    self.q.put(("card", (epc, n)))
                time.sleep(0.1)
        except Exception as e:
            self.q.put(("result", f"✘ 通信异常：{e}（读写器被拔掉？）"))
        finally:
            r.close()

    def _stop(self):
        self.stop_evt.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=1.0)
        self.worker = None

    def _auto_probe(self):
        self._stop()
        self._clear()
        threading.Thread(target=self._probe_all, daemon=True).start()

    def _probe_all(self):
        port = self._port()
        if not port:
            self.q.put(("result", "请先选择串口再自动探测"))
            return
        self.q.put(("log", ("info", f"开始自动探测 {port}：依次尝试 波特率 × 地址 …")))
        for baud in BAUDS:
            for adr in ADDRS:
                if self.stop_evt.is_set():
                    return
                self.q.put(("log", ("info", f"── 尝试 {port} @ {baud} 地址 {adr} ──")))
                try:
                    r = UHFReader18(port, baud, adr, timeout=0.8)
                    r.open()
                    info = r.get_reader_info()
                    r.close()
                except Exception as e:
                    self.q.put(("log", ("err", f"   打开失败：{e}")))
                    continue
                if info:
                    self.q.put(("result", f"✔ 探测成功！串口 {port}，波特率 {baud}，地址 {adr}，"
                                          f"版本 {info.get('version')}。请在主程序【设置】里照此填写。"))
                    self.q.put(("log", ("ok", f"✔ 命中：{baud} / 地址 {adr} / {info}")))
                    return
                else:
                    self.q.put(("log", ("warn", "   无应答")))
        self.q.put(("result", "✘ 所有组合都没应答。检查：驱动、供电、串口是否被占用、"
                              "读写器是否被设置成韦根/主动输出等其他模式。"))

    # ---------------- 消息泵 ----------------
    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    tag, msg = payload
                    self._log(tag, msg)
                elif kind == "result":
                    self.var_result.set(payload)
                    self._log("ok" if payload.startswith("✔") else "warn", payload)
                elif kind == "card":
                    epc, n = payload
                    self._log("ok", f"[读数] 卡号(EPC) = {epc}   第 {n} 次")
                    self.var_result.set(f"✔ 读到卡号：{epc}")
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _on_close(self):
        self._stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    DiagApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
