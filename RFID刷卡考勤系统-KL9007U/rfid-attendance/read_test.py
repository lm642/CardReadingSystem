# -*- coding: utf-8 -*-
"""
====================================================================
 read_test.py —— 命令行读卡自测（最直接的"能不能读卡"验证）
====================================================================
 先用它确认硬件通了，再启动带界面的主程序，排错会快很多。

 用法（在项目文件夹里打开命令行）：
   python read_test.py                      # 自动找串口，波特率 57600
   python read_test.py COM9                 # 指定串口
   python read_test.py COM9 57600           # 指定串口 + 波特率
   python read_test.py COM9 57600 255       # 指定串口 + 波特率 + 读写器地址(0x00/0xFF)
   python read_test.py --list               # 只列出串口
   python read_test.py --info               # 只做一次"握手"读取读写器信息

 退出：按 Ctrl + C
====================================================================
"""
from __future__ import annotations

import sys
import time

import uhf_reader
from uhf_reader import UHFReader18, UHFReaderError, list_serial_ports, guess_reader_port


def cmd_list():
    ports = list_serial_ports()
    if not ports:
        print("没有检测到任何串口。")
        print("请确认：① 读写器已插好；② 装了 CH340/CP210x 驱动（开发包里带）；")
        print("        ③ 设备管理器里能看到 COM 口。")
        return
    print("检测到以下串口：")
    for dev, desc, man in ports:
        print(f"  {dev:<8} {desc}   [{man}]")
    print(f"\n自动识别建议使用： {guess_reader_port() or '无'}")


def cmd_info(port, baud, adr):
    r = UHFReader18(port, baud, adr, verbose=True)
    r.open()
    print("正在读取读写器信息（握手）……")
    info = r.get_reader_info()
    if info is None:
        print("\n✘ 读写器没有应答。可能原因：")
        print("  · 波特率不对（本机常用 57600，可再试 9600 / 115200）")
        print("  · 读写器地址不是 0x00（可用 255 即 0xFF 广播地址再试）")
        print("  · 串口被别的程序占用（比如演示软件 UHFReader18CSharp 还开着，先关掉）")
        print("  · 读写器没上电 / 天线没接")
        r.close()
        return False
    print("\n✔ 读写器在线：")
    for k, v in info.items():
        print(f"    {k}: {v}")
    r.close()
    return True


def cmd_scan(port, baud, adr):
    print(f"打开 {port} @ {baud}，读写器地址 0x{adr:02X} ……  按 Ctrl+C 退出\n")
    r = UHFReader18(port, baud, adr, verbose=False)
    r.open()
    info = r.get_reader_info()
    if info is None:
        print("⚠ 读写器无应答，仍继续尝试询查（也许只是不支持读信息命令）……")
    else:
        print(f"✔ 读写器在线：类型 {info.get('type')} 版本 {info.get('version')} "
              f"功率 {info.get('power')} 频段 {info.get('band')}\n")

    print("请把标签/卡放到读写器天线上……\n")
    n = 0
    last = ""
    try:
        while True:
            tags = r.inventory()
            if tags is None:
                print("⚠ 本条询查无应答（检查连接）")
            for epc in tags or []:
                n += 1
                mark = "  <-- 与上次相同" if epc == last else ""
                print(f"[{time.strftime('%H:%M:%S')}] #{n:<4} 卡号(EPC) = {epc}{mark}")
                last = epc
            time.sleep(0.05)
    except KeyboardInterrupt:
        print(f"\n已停止。共读到 {n} 次。")
    except UHFReaderError as e:
        print(f"\n✘ 通信异常：{e}")
    finally:
        r.close()


def main(argv):
    if "--list" in argv:
        cmd_list()
        return
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return

    port = argv[0] if argv and not argv[0].startswith("-") else guess_reader_port()
    baud = int(argv[1]) if len(argv) > 1 else 57600
    adr = int(argv[2]) if len(argv) > 2 else 0x00

    if not port:
        print("✘ 没有找到串口。请先运行：python read_test.py --list")
        return

    if "--info" in argv:
        cmd_info(port, baud, adr)
    else:
        cmd_scan(port, baud, adr)


if __name__ == "__main__":
    main(sys.argv[1:])
