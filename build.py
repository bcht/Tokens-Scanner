"""自检环境 + 打单文件 exe。

写给刚 clone 下来的人:缺什么就直说缺什么、怎么补,而不是让 PyInstaller
在打包到一半时报一屏看不懂的错。

    python build.py           自检通过就打包
    python build.py --check    只自检,不打包

只在 Windows 上有效 —— 产物是 Windows 的单文件 GUI exe。
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXE_NAME = "Tokens-Scanner"
EXE_PATH = os.path.join(HERE, "dist", EXE_NAME + ".exe")

# scanner.py 解压 DSH 会话时优先用标准库 compression.zstd —— 那是 3.14 起才有的。
# 更早的 Python 上要另装 zstandard 走回退分支。
MIN_PYTHON = (3, 14)
FALLBACK = "zstandard"

# 打包需要的东西:(import 名, 给人看的名字)
PACKAGES = [
    ("webview", "pywebview"),
    ("PyInstaller", "PyInstaller"),
]

# 和以前 build.bat 里那份完全一致,别改
PYINSTALLER_ARGS = [
    "--noconfirm",
    "--onefile",
    "--windowed",
    "--name", EXE_NAME,
    "--add-data", "ui;ui",
    "--hidden-import", "webview.platforms.edgechromium",
    "--hidden-import", "webview.platforms.winforms",
    "--hidden-import", "clr_loader",
    "main.py",
]


def _installed(module):
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _version(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "?"


def run_check():
    """返回 (通过的行, 缺东西的说明)。

    缺什么不必逐条给命令 —— requirements.txt 里都写好了(包括按 Python 版本
    条件安装的 zstandard),一条 pip install -r 就能补齐。
    """
    ok, missing = [], []

    version = sys.version.split()[0]
    if sys.version_info >= MIN_PYTHON:
        ok.append(f"Python {version}")
    elif _installed(FALLBACK):
        ok.append(f"Python {version}(比 {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 旧,靠 {FALLBACK} 兜着)")
    else:
        missing.append(
            f"Python {version} 太旧 —— 需要 {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更新"
            f"(解压 DSH 会话要用标准库 compression.zstd)"
        )

    for module, package in PACKAGES:
        if _installed(module):
            ok.append(f"{package} {_version(package)}")
        else:
            missing.append(f"缺 {package}")

    return ok, missing


def main():
    if os.name != "nt":
        print("这个脚本只在 Windows 上有效 —— 产物是 Windows 的单文件 exe。")
        return 1

    print("环境自检")
    ok, missing = run_check()
    for line in ok:
        print(f"  [ok] {line}")
    for line in missing:
        print(f"  [!!] {line}")

    if missing:
        print()
        print("在项目目录里跑这一条就能补齐(缺什么它装什么):")
        print("    pip install -r requirements.txt")
        return 1

    if "--check" in sys.argv:
        print()
        print("自检通过。去掉 --check 就开始打包。")
        return 0

    data_json = os.path.join(HERE, "ui", "data.json")
    if os.path.exists(data_json):
        os.remove(data_json)
        print("  已删掉 ui/data.json(浏览器调试用的缓存,打进正式包的话"
              "桥接一旦失败会拿构建那一刻的旧数据冒充当前数据)")

    print()
    print("打包中 —— 第一次会慢一些,PyInstaller 要收集依赖。")
    # 记下改前的时间戳:源码没变的话 PyInstaller 会复用上次的构建、不重写 exe,
    # 那时候产物是旧的但内容是对的 —— 不说清楚容易被当成没打包成功
    before = os.path.getmtime(EXE_PATH) if os.path.exists(EXE_PATH) else None

    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", *PYINSTALLER_ARGS], cwd=HERE
    )

    if result.returncode != 0:
        print()
        print(f"打包失败(PyInstaller 退出码 {result.returncode})。"
              f"上面最后几行通常就是原因。")
        return result.returncode

    if not os.path.exists(EXE_PATH):
        print()
        print("PyInstaller 说自己成功了,但没找到产物:")
        print("   ", EXE_PATH)
        return 1

    size = os.path.getsize(EXE_PATH) / (1024 * 1024)
    print()
    print(f"好了。产物:{EXE_PATH}")
    print(f"            {size:.1f} MB")
    if os.path.getmtime(EXE_PATH) == before:
        print("            (没重写 —— 源码和上次一样,PyInstaller 复用了上次的构建)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
