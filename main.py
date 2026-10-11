"""Tokens Scanner —— 桌面窗口入口。

日志默认就写,放在 exe 旁边(目录不可写时退到 %LOCALAPPDATA%)。
加 `--debug` 会降到 DEBUG 级别 —— 逐个文件、每次 JS 调用都记下来 ——
并且在有控制台时同步打印。

为什么默认就写:打包成 windowed 的 exe 没有控制台,启动失败时是
「双击没反应」——没日志就等于什么都没留下,只能靠猜。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback

import webview

import applog
import scanner
from cache import Cache, default_db_path, program_dir

APP_VERSION = "0.1.0"


def resource_path(*parts):
    """开发时指向源码目录;打包成 exe 后指向 PyInstaller 解压出的临时目录。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def _install_excepthooks():
    """兜住没被捕获的异常 —— 否则它只会杀掉进程,什么痕迹都不留。"""

    def hook(exc_type, exc, tb):
        applog.log.critical(
            "未捕获的异常:\n%s", "".join(traceback.format_exception(exc_type, exc, tb))
        )

    sys.excepthook = hook

    def thread_hook(args):
        name = args.thread.name if args.thread else "?"
        applog.log.critical(
            "线程 %s 未捕获的异常:\n%s",
            name,
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
        )

    threading.excepthook = thread_hook


class Api:
    """暴露给前端 JS 的接口。

    扫描会被窗口初次加载和自动刷新反复调用,加锁避免重入;
    存档本身是按文件增量的,所以重复调用很便宜。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = Cache()
        self._calls = 0

    def load(self):
        self._calls += 1
        started = time.perf_counter()
        with self._lock:
            payload, stats = scanner.collect(self._cache)
            payload["scan"] = {
                "seconds": round(stats["seconds"], 3),
                "files": stats["total_files"],
                "reused": stats["reused"],
                "parsed": stats["parsed"],
                "archived": stats["archived"],
                "db": stats["cache_path"] or "",
                "db_error": stats["cache_error"],
                "db_backup": stats["db_backup"],
                "db_migrated": stats["db_migrated"],
            }

        applog.log.info(
            "取数 #%d  用时 %.3fs  文件 %d(复用 %d / 新解析 %d / 已归档 %d)%s",
            self._calls,
            time.perf_counter() - started,
            stats["total_files"],
            stats["reused"],
            stats["parsed"],
            stats["archived"],
            f"  [数据库异常: {stats['cache_error']}]" if stats["cache_error"] else "",
        )
        return payload

    def rescan(self):
        return self.load()


def main():
    debug = "--debug" in sys.argv

    # 日志和存档放同一处 —— 存档能写的地方,日志就能写
    log_dir = os.path.dirname(default_db_path())
    log_file = applog.setup(log_dir, debug=debug)

    applog.log.info("=" * 64)
    applog.log.info("Tokens Scanner %s 启动", APP_VERSION)
    applog.log.info("  打包运行    %s", bool(getattr(sys, "frozen", False)))
    applog.log.info("  可执行文件  %s", sys.executable)
    applog.log.info("  Python      %s", sys.version.split()[0])
    applog.log.info("  程序目录    %s", program_dir())
    applog.log.info("  日志文件    %s", log_file or "(目录不可写,只剩控制台输出)")
    applog.log.info("  命令行      %s", sys.argv)
    _install_excepthooks()

    try:
        window = webview.create_window(
            "Tokens Scanner",
            resource_path("ui", "index.html"),
            js_api=Api(),
            width=1460,
            height=940,
            min_size=(1080, 700),
            background_color="#F4F1EA",
        )
        applog.log.info("窗口已创建,交给 webview.start()")
        started = time.perf_counter()
        webview.start()
        applog.log.info("窗口已关闭(webview.start 返回),运行了 %.1fs", time.perf_counter() - started)
    except Exception as exc:  # noqa: BLE001 - 这里就是要兜住一切
        applog.log.critical("启动失败:\n%s", traceback.format_exc())
        # 光写日志不够 —— 用户不知道日志在哪。双击没反应时,这个框就是唯一线索
        applog.show_fatal(
            "Tokens Scanner 启动失败",
            f"{type(exc).__name__}: {exc}\n\n详细日志:\n{log_file or '(无法写入日志文件)'}",
        )
        sys.exit(1)
    finally:
        applog.log.info("进程退出")
    return window


if __name__ == "__main__":
    main()
