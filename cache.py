"""按文件的 SQLite 存档。

会话文件会长到几百兆,全量解析一次要好几秒;而自动刷新每分钟都会跑一遍。
这里记下每个文件的解析结果,只有真正变过的文件才重新解析。

**它是存档,不是纯缓存**:磁盘上已经消失的会话文件,它的记录仍然留着 ——
否则你清理掉旧会话之后,那些天的用量会直接从统计里消失。

主键是**会话标识**而不是路径:文件被挪了位置、改了目录名,也仍然认得出是同一个会话,
不会因为路径变了就多出一条记录、把用量算两遍。

库放在程序同目录下的 usage-cache.db;那个目录不可写时(比如装在 Program Files 里)
退到 %LOCALAPPDATA%\\TokenUsage\\。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading

DB_NAME = "usage-cache.db"

# 主键和记录格式一变,旧行就没法直接读了。但也不能直接丢掉 ——
# 磁盘上已经消失的会话只存在于库里。所以重建前会:
#   ① 用 VACUUM INTO 把旧库整体另存一份(兜底,人工可恢复)
#   ② 把旧表改名为 file_cache_prev,尽量把还能读的记录搬进新表
CACHE_VERSION = 3
RECORD_FIELDS = 9  # [来源, 模型, 日期, 输入, 输出, 缓存读, 缓存写, 会话, 消息id]

META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_cache (
    session_key TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    source      TEXT NOT NULL,
    mtime       REAL NOT NULL,
    size        INTEGER NOT NULL,
    records     TEXT NOT NULL,
    scanned_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def program_dir():
    """打包成 exe 后指向 exe 所在目录,开发时指向源码目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _writable(directory):
    probe = os.path.join(directory, ".write-test")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def default_db_path():
    """优先放在程序旁边;放不下才退到用户目录。"""
    beside = program_dir()
    if _writable(beside):
        return os.path.join(beside, DB_NAME)

    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    fallback = os.path.join(local, "TokenUsage")
    os.makedirs(fallback, exist_ok=True)
    return os.path.join(fallback, DB_NAME)


def session_key_for(parser_name, path):
    """会话标识。用会话本身做键,而不是路径 —— 文件被挪了位置、改了目录名,
    仍然认得出是同一个会话,不会多出一条记录把用量算两遍。"""
    if parser_name == "parse_dsh_file":
        return "dsh:" + os.path.basename(os.path.dirname(path))
    if parser_name == "parse_claude_file":
        return "claude:" + os.path.splitext(os.path.basename(path))[0]
    return "codex:" + os.path.splitext(os.path.basename(path))[0]


def _records_look_current(parsed):
    """记录格式没变才敢搬。变了就跳过,让它在备份里躺着。"""
    if not isinstance(parsed, list):
        return False
    for item in parsed[:1]:
        if not isinstance(item, list) or len(item) != RECORD_FIELDS:
            return False
    return True


class Cache:
    """一个薄薄的 SQLite 包装。打不开就当没有缓存,不要因此让程序起不来。

    pywebview 会在不同的工作线程上调用 JS API,而 sqlite3 默认禁止连接跨线程使用
    (check_same_thread),一旦轮询落到别的线程上就会静默失效、每次都退回全量扫描。
    所以这里关掉那个检查,并用一把自己的锁串行化访问。
    """

    def __init__(self, path=None):
        self.path = path or default_db_path()
        self.conn = None
        self.hits = 0
        self.fresh = 0
        self.error = None
        # 版本不匹配时旧库被另存到了哪里(只报一次,见 scanner.collect)
        self.archived_to = None
        # 版本升级时成功搬过来的旧记录条数
        self.migrated = 0
        self._lock = threading.RLock()

    def open(self):
        with self._lock:
            if self.conn is not None:
                return True
            self.error = None
            try:
                with open(self.path, "a", encoding="utf-8"):
                    pass
                self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
                self.conn.executescript(META_SCHEMA)

                stored = self._stored_version()
                if stored == str(CACHE_VERSION):
                    self.conn.executescript(FILE_SCHEMA)
                else:
                    self._rebuild(stored)

                self.conn.commit()
            except (OSError, sqlite3.Error) as exc:
                self.conn = None
                self.error = str(exc)
            return self.conn is not None

    def _rebuild(self, stored):
        """版本对不上:先兜底另存,再把旧表改名、建新表、尽量把能读的记录搬过来。

        为什么不直接 DROP:磁盘上已经没有的会话,记录只存在这里,丢了就找不回来。
        为什么不只 DELETE 行:列本身也可能变了,而 CREATE TABLE IF NOT EXISTS
        不会改动已存在的表 —— 写入会因列对不上而静默失败,存档表面正常实际全废。
        """
        had_data = self._has_archive_data()
        if had_data:
            self._archive_before_reset(stored or "旧版")
            self.conn.executescript("DROP TABLE IF EXISTS file_cache_prev;")
            self.conn.execute("ALTER TABLE file_cache RENAME TO file_cache_prev")

        self.conn.executescript(FILE_SCHEMA)
        self.migrated = self._migrate_prev() if had_data else 0
        self.conn.executescript("DROP TABLE IF EXISTS file_cache_prev;")
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES ('version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(CACHE_VERSION),),
        )

    def _migrate_prev(self):
        """把旧表里还能按当前格式读出来的记录搬进新表。
        读不出来就跳过 —— 另存的那份备份里有完整的旧库,不至于丢。"""
        try:
            rows = self.conn.execute(
                "SELECT path, source, mtime, size, records FROM file_cache_prev"
            ).fetchall()
        except sqlite3.Error:
            return 0

        moved = 0
        for path, source, mtime, size, records in rows:
            try:
                parsed = json.loads(records)
            except (ValueError, TypeError):
                continue
            if not _records_look_current(parsed):
                continue
            self.conn.execute(
                "INSERT OR IGNORE INTO file_cache "
                "(session_key, path, source, mtime, size, records) VALUES (?, ?, ?, ?, ?, ?)",
                (session_key_for(source, path), path, source, mtime, size, records),
            )
            moved += 1
        return moved

    def _stored_version(self):
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        except sqlite3.Error:
            return None
        return row[0] if row else None

    def _has_archive_data(self):
        """库里是否真有过东西。空库没必要另存。"""
        try:
            row = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='file_cache'"
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    def _archive_before_reset(self, stamp):
        """用 VACUUM INTO 写一份干净副本 —— 比直接改名省事,
        不用操心 -journal / -wal 这些边车文件,而且存出来的是完整一致的库。"""
        target = self._free_path(f"usage-cache.{stamp}.db")
        try:
            self.conn.execute("VACUUM INTO ?", (target,))
        except sqlite3.Error as exc:
            # 另存失败不能连累主流程:原库还在,照常重建就是
            self.error = f"旧库另存失败(原库仍会重建):{exc}"
            return
        self.archived_to = target

    def _free_path(self, name):
        base = os.path.dirname(self.path)
        stem, ext = os.path.splitext(name)
        target = os.path.join(base, name)
        index = 2
        while os.path.exists(target):
            target = os.path.join(base, f"{stem}-{index}{ext}")
            index += 1
        return target

    def entries(self):
        """一次读全表,按会话标识索引。文件数量是几十到几千,分开查反而更慢。"""
        with self._lock:
            if self.conn is None:
                return {}
            try:
                rows = self.conn.execute(
                    "SELECT session_key, path, mtime, size, records FROM file_cache"
                ).fetchall()
            except sqlite3.Error as exc:
                self.error = str(exc)
                return {}
            out = {}
            for key, path, mtime, size, records in rows:
                try:
                    out[key] = (path, mtime, size, json.loads(records))
                except (ValueError, TypeError):
                    continue  # 这一行坏了就当没有,下次重算
            return out

    def put(self, session_key, path, source, mtime, size, records):
        with self._lock:
            if self.conn is None:
                return
            try:
                self.conn.execute(
                    "INSERT INTO file_cache (session_key, path, source, mtime, size, records) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_key) DO UPDATE SET "
                    "path=excluded.path, source=excluded.source, mtime=excluded.mtime, "
                    "size=excluded.size, records=excluded.records, scanned_at=datetime('now')",
                    (session_key, path, source, mtime, size,
                     json.dumps(records, ensure_ascii=False)),
                )
            except sqlite3.Error as exc:
                self.error = str(exc)

    def commit(self):
        with self._lock:
            if self.conn is not None:
                try:
                    self.conn.commit()
                except sqlite3.Error as exc:
                    self.error = str(exc)

    # ---- 设置项:复用 meta 表,免得为了一点偏好再开一个文件 ----

    def get_setting(self, key, default=None):
        with self._lock:
            if self.conn is None:
                return default
            try:
                row = self.conn.execute(
                    "SELECT value FROM meta WHERE key = ?", (f"setting:{key}",)
                ).fetchone()
            except sqlite3.Error:
                return default
            return row[0] if row else default

    def set_setting(self, key, value):
        with self._lock:
            if self.conn is None:
                return False
            try:
                self.conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (f"setting:{key}", str(value)),
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                self.error = str(exc)
                return False
            return True

    def close(self):
        with self._lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                except sqlite3.Error:
                    pass
                self.conn = None
