# -*- coding: utf-8 -*-
"""
====================================================================
 环境自检小工具  env_check.py
 ------------------------------------------------------------------
 作用：打印"当前这个 Python 解释器"的运行环境报告 —— 解释器是哪只、
       有没有 tkinter 图形界面、有没有 pyserial 串口库（系统装的 / 项目
       内置的）、系统里有哪些串口。

 它本身不依赖任何第三方库，可以被"环境自检.bat"用不同 Python 反复调用，
 一次性把一台电脑上多个 Python 的差异全部列出来 ——
 这是排查"我明明装了 pyserial，程序却说没装"最直接的办法。

 直接运行： python env_check.py
====================================================================
"""
import os
import sys
import importlib

BASE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(BASE, "_vendor")


def can_import(mod, extra_path=None):
    """能不能导入某个模块（可选临时插入一条搜索路径）。"""
    old = list(sys.path)
    if extra_path and os.path.isdir(extra_path):
        sys.path.insert(0, extra_path)
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False
    finally:
        sys.path[:] = old


def module_version(mod):
    try:
        m = importlib.import_module(mod)
        return getattr(m, "__version__", "已安装")
    except Exception:
        return "-"


def main():
    sys_ser = can_import("serial")
    vend_ser = can_import("serial", VENDOR)

    line = []
    line.append("解释器      : " + sys.executable)
    line.append("版本        : " + sys.version.split()[0])
    line.append("tkinter     : " + ("可用" if can_import("tkinter") else "【缺失】"))
    line.append("pyserial系统: " + (f"可用 v{module_version('serial')}" if sys_ser else "【缺失】"))
    line.append("pyserial内置: " + (f"可用 v{module_version('serial')}" if vend_ser else "【缺失】"))

    # 结论
    usable = vend_ser or sys_ser
    line.append("结论        : " + ("✅ 这个解释器可以正常跑本项目"
                                     if usable else "❌ 缺少串口库，请检查 _vendor 目录"))

    # 串口列表（用能找到的 pyserial）
    if usable:
        if vend_ser:
            sys.path.insert(0, VENDOR)
        from serial.tools import list_ports
        ports = [f"{p.device} ({p.description})" for p in list_ports.comports()]
        line.append("串口        : " + ("；".join(ports) if ports else "无"))
    else:
        line.append("串口        : 无法枚举")

    print("\n".join(line))
    print("-" * 62)


if __name__ == "__main__":
    main()
