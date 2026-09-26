"""SQLite 存储层。

同步驱动 sqlite3，每请求一个连接。金额以整数分存储，
刷卡时刻以 'YYYY-MM-DD HH:MM:SS' 文本存储（可直接排序）。
备份走 SQLite 在线备份 API：先落临时文件、通过完整性检查后再提供下载。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

from .core import Config, Fare, Station, Swipe

DB_PATH = os.environ.get("DB_PATH", "/data/bus.db")

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _yuan(cents: int) -> float:
    return round(cents / 100.0, 2)


def _cents(yuan: float) -> int:
    return int(round(float(yuan) * 100))


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE,
    zone  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fare (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_zone  INTEGER NOT NULL,
    to_zone    INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    UNIQUE(from_zone, to_zone)
);

CREATE TABLE IF NOT EXISTS swipe (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    card     TEXT NOT NULL,
    t        TEXT NOT NULL,
    station  TEXT NOT NULL,
    action   TEXT NOT NULL,        -- IN / OUT
    src_line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_swipe_card_t ON swipe(card, t);

CREATE TABLE IF NOT EXISTS trip_bill (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    card             TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    operating_day    TEXT NOT NULL,
    entry_time       TEXT NOT NULL,
    exit_time        TEXT NOT NULL,
    entry_station    TEXT NOT NULL,
    exit_station     TEXT NOT NULL,
    from_zone        INTEGER NOT NULL,
    to_zone          INTEGER NOT NULL,
    base_cents       INTEGER NOT NULL,
    discount_cents   INTEGER NOT NULL,
    charge_cents     INTEGER NOT NULL,
    cumulative_cents INTEGER NOT NULL,
    cap_cents        INTEGER NOT NULL,
    discount_source  TEXT NOT NULL,
    entry_line       INTEGER NOT NULL,
    exit_line        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bill_card ON trip_bill(card, seq);
"""

DEFAULT_CONFIG = {
    "transfer_minutes": "90",
    "transfer_discount": "1.00",
    "daily_cap": "10.00",
    "day_split_hour": "4",
}


@contextmanager
def connect(db_path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    path = db_path or DB_PATH
    _ensure_parent(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_CONFIG.items():
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (k, v))


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


def get_config(conn: sqlite3.Connection) -> Config:
    def g(key: str, default: str) -> str:
        v = get_meta(conn, key)
        return v if v is not None else default

    return Config(
        transfer_minutes=int(g("transfer_minutes", DEFAULT_CONFIG["transfer_minutes"])),
        transfer_discount=float(g("transfer_discount", DEFAULT_CONFIG["transfer_discount"])),
        daily_cap=float(g("daily_cap", DEFAULT_CONFIG["daily_cap"])),
        day_split_hour=int(g("day_split_hour", DEFAULT_CONFIG["day_split_hour"])),
    )


def update_config(conn: sqlite3.Connection, cfg: Config) -> None:
    set_meta(conn, "transfer_minutes", str(cfg.transfer_minutes))
    set_meta(conn, "transfer_discount", f"{cfg.transfer_discount:.2f}")
    set_meta(conn, "daily_cap", f"{cfg.daily_cap:.2f}")
    set_meta(conn, "day_split_hour", str(cfg.day_split_hour))


# ---------------------------------------------------------------------------
# 基础数据：整表替换（原子事务，错误时由调用方不调用）
# ---------------------------------------------------------------------------

def replace_stations(conn: sqlite3.Connection, stations: list[Station]) -> None:
    conn.execute("DELETE FROM station")
    conn.executemany(
        "INSERT INTO station(name, zone) VALUES (?, ?)",
        [(s.name, s.zone) for s in stations])


def replace_fares(conn: sqlite3.Connection, fares: list[Fare]) -> None:
    conn.execute("DELETE FROM fare")
    conn.executemany(
        "INSERT INTO fare(from_zone, to_zone, price_cents) VALUES (?, ?, ?)",
        [(f.from_zone, f.to_zone, _cents(f.price)) for f in fares])


def replace_records(conn: sqlite3.Connection, records: list[Swipe]) -> None:
    conn.execute("DELETE FROM swipe")
    conn.executemany(
        "INSERT INTO swipe(card, t, station, action, src_line) "
        "VALUES (?, ?, ?, ?, ?)",
        [(r.card, r.t.strftime(TIME_FMT), r.station, r.action, r.line)
         for r in records])


def save_bills(conn: sqlite3.Connection, bills: list[dict]) -> None:
    """一次核验产出：先清空旧账单，再写入新账单（同一事务）。"""
    conn.execute("DELETE FROM trip_bill")
    conn.executemany(
        """INSERT INTO trip_bill(card, seq, operating_day, entry_time, exit_time,
               entry_station, exit_station, from_zone, to_zone,
               base_cents, discount_cents, charge_cents, cumulative_cents,
               cap_cents, discount_source, entry_line, exit_line)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(b["card"], b["seq"], b["operating_day"], b["entry_time"],
          b["exit_time"], b["entry_station"], b["exit_station"],
          b["from_zone"], b["to_zone"],
          _cents(b["base_fare"]), _cents(b["discount"]),
          _cents(b["charged_fare"]), _cents(b["daily_cumulative"]),
          _cents(b["daily_cap"]), b["discount_source"],
          b["entry_line"], b["exit_line"]) for b in bills])
    set_meta(conn, "last_verified_at",
             datetime.now().strftime(TIME_FMT))


def clear_bills(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM trip_bill")
    conn.execute("DELETE FROM meta WHERE key='last_verified_at'")


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def list_stations(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, zone FROM station ORDER BY zone, name").fetchall()
    return [dict(r) for r in rows]


def list_fares(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, from_zone, to_zone, price_cents "
        "FROM fare ORDER BY from_zone, to_zone").fetchall()
    return [{"id": r["id"], "from_zone": r["from_zone"],
             "to_zone": r["to_zone"], "price": _yuan(r["price_cents"])}
            for r in rows]


def list_records(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, card, t, station, action, src_line "
        "FROM swipe ORDER BY id").fetchall()
    return [{"id": r["id"], "card": r["card"], "time": r["t"],
             "station": r["station"], "action": r["action"],
             "src_line": r["src_line"]} for r in rows]


def load_domain(conn: sqlite3.Connection):
    """读出 core 层所需的全部输入数据。"""
    stations = [Station(name=r["name"], zone=r["zone"])
                for r in conn.execute("SELECT name, zone FROM station")]
    fares = [Fare(from_zone=r["from_zone"], to_zone=r["to_zone"],
                  price=_yuan(r["price_cents"]))
             for r in conn.execute(
                 "SELECT from_zone, to_zone, price_cents FROM fare")]
    records: list[Swipe] = []
    for r in conn.execute(
            "SELECT card, t, station, action, src_line FROM swipe ORDER BY id"):
        records.append(Swipe(
            card=r["card"],
            t=datetime.strptime(r["t"], TIME_FMT),
            station=r["station"], action=r["action"], line=r["src_line"]))
    return stations, fares, records


def list_bills(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM trip_bill ORDER BY card, seq, id""").fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "card": r["card"], "seq": r["seq"],
            "operating_day": r["operating_day"],
            "entry_time": r["entry_time"], "exit_time": r["exit_time"],
            "entry_station": r["entry_station"],
            "exit_station": r["exit_station"],
            "from_zone": r["from_zone"], "to_zone": r["to_zone"],
            "base_fare": _yuan(r["base_cents"]),
            "discount": _yuan(r["discount_cents"]),
            "charged_fare": _yuan(r["charge_cents"]),
            "daily_cumulative": _yuan(r["cumulative_cents"]),
            "daily_cap": _yuan(r["cap_cents"]),
            "discount_source": r["discount_source"],
            "entry_line": r["entry_line"], "exit_line": r["exit_line"],
        })
    return out


# ---------------------------------------------------------------------------
# 数据库备份（在线快照，只读）
# ---------------------------------------------------------------------------

# 快照必须覆盖的数据表：配置(meta)、站点、票价、刷卡记录与最近账单
BACKUP_REQUIRED_TABLES = ("meta", "station", "fare", "swipe", "trip_bill")


def create_backup_snapshot(db_path: Optional[str] = None) -> str:
    """生成数据库一致性快照，返回临时文件路径（调用方负责删除）。

    - 使用 SQLite 在线备份 API（Connection.backup）：备份期间即使有其他
      连接并发写入，快照也严格对应某一个完整提交点；绝不直接复制正在
      使用的数据库文件（直接复制可能拿到写了一半的页面）；
    - 源连接以只读模式（mode=ro）打开，备份流程不修改任何业务数据；
    - 快照先写入临时文件，通过 PRAGMA integrity_check 与必备表核对后
      才返回路径；任一步失败都会清理临时文件并抛出异常。
    """
    path = db_path or DB_PATH
    fd, tmp_path = tempfile.mkstemp(prefix="bus-backup-", suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(tmp_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        check = sqlite3.connect(tmp_path)
        try:
            rows = check.execute("PRAGMA integrity_check").fetchall()
            if not (len(rows) == 1 and rows[0][0] == "ok"):
                raise RuntimeError("备份快照完整性检查未通过")
            tables = {r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in BACKUP_REQUIRED_TABLES if t not in tables]
            if missing:
                raise RuntimeError(
                    "备份快照缺少数据表：" + ", ".join(missing))
        finally:
            check.close()
    except Exception:
        # 失败时不留下半成品临时文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path
