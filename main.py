"""Tokens Scanner —— 桌面窗口入口。"""

from __future__ import annotations

import os
import sys
import threading

import webview

import scanner
from cache import Cache


def resource_path(*parts):
    """开发时指向源码目录;打包成 exe 后指向 PyInstaller 解压出的临时目录。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


class Api:
    """暴露给前端 JS 的接口。

    扫描会被窗口初次加载和自动刷新反复调用,加锁避免重入;
    缓存本身是按文件增量的,所以重复调用很便宜。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = Cache()

    def load(self):
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
            return payload

    def rescan(self):
        return self.load()


def main():
    window = webview.create_window(
        "Tokens Scanner",
        resource_path("ui", "index.html"),
        js_api=Api(),
        width=1460,
        height=940,
        min_size=(1080, 700),
        background_color="#F4F1EA",
    )
    webview.start()
    return window


if __name__ == "__main__":
    main()
