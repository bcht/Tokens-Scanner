"""日志。

打包成 windowed 的 exe 之后没有控制台,出错会无声无息地消失 ——
所以**默认就写文件**,不用加参数。`--debug` 只是把级别降到 DEBUG
(记下逐个文件、每次 JS 调用的细节),并尽量把日志同时输出到控制台。

pywebview 自己用 logging,所以配的是 root logger,它的消息
(比如端口被占用)也会一并落进来。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

LOG_FILENAME = "tokens-scanner.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 1

log = logging.getLogger("tokens")

FILE_FORMAT = "%(asctime)s  %(levelname)-5s  %(name)-16s %(message)s"
CONSOLE_FORMAT = "%(asctime)s  %(levelname)-5s  %(message)s"


def _attach_console():
    """windowed 的 exe 默认没有 stdout。--debug 时尽量挂到父进程的控制台上,
    这样在终端里运行就能实时看到日志。

    AttachConsole(-1) = ATTACH_PARENT_PROCESS。父进程没有控制台就失败,
    那就只剩文件日志 —— 不影响运行。

    按控制台**当前的代码页**编码输出,而不是一律 UTF-8 ——
    否则 Windows 默认的 GBK 控制台会把中文显示成乱码。
    """
    if sys.stdout is not None:
        return
    try:
        import ctypes

        if not ctypes.windll.kernel32.AttachConsole(-1):
            return
        codepage = ctypes.windll.kernel32.GetConsoleOutputCP()
        encoding = f"cp{codepage}" if codepage else "utf-8"
        sys.stdout = open("CONOUT$", "w", encoding=encoding, errors="replace", buffering=1)
        sys.stderr = sys.stdout
    except Exception:
        pass


def setup(directory, debug=False):
    """装好日志,返回日志文件的路径(装不上就返回 None)。

    整个过程包在 try 里:日志出问题绝不能让程序起不来。
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for existing in list(root.handlers):
        root.removeHandler(existing)

    path = os.path.join(directory, LOG_FILENAME)
    try:
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(FILE_FORMAT))
        root.addHandler(handler)
    except OSError:
        path = None

    if debug:
        _attach_console()
        if sys.stdout is not None:
            stream = logging.StreamHandler(sys.stdout)
            stream.setFormatter(logging.Formatter(CONSOLE_FORMAT))
            root.addHandler(stream)
        log.debug("调试模式已开启")

    return path


def show_fatal(title, message):
    """启动失败时弹一个原生对话框。

    光写日志不够 —— 用户不知道日志在哪。窗口ed 的 exe 崩起来是"双击没反应",
    什么都不弹的话，日志等于没写。
    """
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass
