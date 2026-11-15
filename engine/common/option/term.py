"""
オプションの満期までの時間(スワップション・国債店頭オプションで共通)。

満期までの時間はここだけで数える。プレミアム→volの換算(swaption/convert.py)、ボラの格子の補間
(bond/vol.py)、現物オプションの期間T・セータ(bond/pricer.py)、BondOptionシートのT(表UDF
OptionTermTable)、暦日ベースへの掛け目は全てここを使う。volを割った日数と、プライサーに渡すTの日数が
ずれると、同じプレミアムを再現できなくなるため。

■ 数え方(単位は営業日)
  満期日の expiry_time(15時)に残り0。1営業日 = 前の営業日の15時 → 当日の15時(ユーザー決定、2026-11-15)。
  - 日付(date)を渡したとき: その日の15時(引け)時点。残り = 取引日から満期日の前日までの営業日数
    (ql.Japan()、当日を1日と数える)。ヒストリカル(引け値は15時の値)・過去の評価日・朝バッチはこちら。
  - 時刻(datetime)を渡したとき(ライブ): 残り = (今いる1営業日の当日から満期日の前日までの営業日数)
    + (今いる1営業日の残りの割合)。割合は option_time.toml の時間帯(イブニング1:日中2、日中は8:45〜15:00)で
    重みをつけ、時間帯の中では時間に比例して減らす。15時ちょうどの値は日付を渡したときと同じ。
  - 年率の日数は1年252営業日。期間 T = 残りの営業日数 / 252。セータの1営業日 = T から 1/252 を引く。
  時刻は日本時間の現地時刻(naive)とする。
"""

import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional, Tuple, Union

import QuantLib as ql

from engine.common.conventions import CALENDAR
from engine.common.paths import REPO_ROOT

TRADING_DAYS = 252  # 年率の日数(プロジェクトの規約。BondOption・VolHVと同じ)
ONE_DAY = 1 / TRADING_DAYS  # セータの1営業日(年)
CONFIG_PATH = REPO_ROOT / "option_time.toml"
SESSION = "session"
DAILY = "daily"

When = Union[date, datetime]


@dataclass(frozen=True)
class Session:
    name: str
    start: time
    end: time
    weight: float


@dataclass(frozen=True)
class TimeConfig:
    expiry_time: time = time(15, 0)
    method: str = SESSION
    sessions: Tuple[Session, ...] = (
        Session("evening", time(15, 30), time(6, 0), 1.0),
        Session("day", time(8, 45), time(15, 0), 2.0),
    )


def _parse_time(value, name: str) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{name} は \"HH:MM\" で書いてください: {value!r}") from None


def _windows(cfg: TimeConfig, prev: date, day: date):
    """前の営業日 prev → 当日 day の1営業日の中の [(始まり, 終わり, 重み)]。"""
    for s in cfg.sessions:
        base = prev if s.start >= cfg.expiry_time else day
        start = datetime.combine(base, s.start)
        end = datetime.combine(base, s.end)
        if end <= start:
            end += timedelta(days=1)
        yield start, end, s.weight


def _validate(cfg: TimeConfig) -> TimeConfig:
    if cfg.method not in (SESSION, DAILY):
        raise ValueError(f"method は \"{SESSION}\" か \"{DAILY}\" です: {cfg.method!r}")
    if cfg.method == DAILY:
        return cfg
    if not cfg.sessions:
        raise ValueError("method = \"session\" には [[session]] を1つ以上書いてください")
    for s in cfg.sessions:
        if not s.weight > 0:
            raise ValueError(f"session {s.name!r} の weight は正の数です: {s.weight}")
    # 平日が続く1営業日(木→金)と週末をまたぐ1営業日(金→月)で、時間帯が1営業日の中に収まり重ならないか
    for prev, day in ((date(2026, 11, 12), date(2026, 11, 13)), (date(2026, 11, 13), date(2026, 11, 16))):
        lo, hi = datetime.combine(prev, cfg.expiry_time), datetime.combine(day, cfg.expiry_time)
        spans = sorted((a, b) for a, b, _ in _windows(cfg, prev, day))
        for a, b in spans:
            if a < lo or b > hi:
                raise ValueError(f"session の時間帯が1営業日(前の営業日の{cfg.expiry_time:%H:%M}〜当日の"
                                 f"{cfg.expiry_time:%H:%M})からはみ出しています: {a:%H:%M}〜{b:%H:%M}")
        for (_, b), (a, _) in zip(spans, spans[1:]):
            if a < b:
                raise ValueError("session の時間帯が重なっています")
    return cfg


def load_config(path: Path = CONFIG_PATH) -> TimeConfig:
    """option_time.toml を読む。ファイルが無ければ既定(TimeConfig())。"""
    if not Path(path).exists():
        return TimeConfig()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    unknown = set(raw) - {"expiry_time", "method", "session"}
    if unknown:
        raise ValueError(f"{Path(path).name} に知らない項目があります: {sorted(unknown)}")
    sessions = []
    for i, s in enumerate(raw.get("session", [])):
        extra = set(s) - {"name", "start", "end", "weight"}
        if extra:
            raise ValueError(f"[[session]] に知らない項目があります: {sorted(extra)}")
        name = str(s.get("name", f"session{i + 1}"))
        try:
            weight = float(s["weight"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"session {name!r} の weight を数値で書いてください") from None
        sessions.append(Session(name, _parse_time(s.get("start"), f"session {name!r} の start"),
                                _parse_time(s.get("end"), f"session {name!r} の end"), weight))
    return _validate(TimeConfig(
        expiry_time=_parse_time(raw.get("expiry_time", "15:00"), "expiry_time"),
        method=str(raw.get("method", SESSION)).strip().lower(),
        sessions=tuple(sessions),
    ))


_config: Optional[TimeConfig] = None


def config() -> TimeConfig:
    """使う設定(プロセスで1回だけ読む。変えたらUDFサーバ・フィードを再起動する)。"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(cfg: Optional[TimeConfig]) -> Optional[TimeConfig]:
    """設定を差し替える(テスト用。None で次の config() が読み直す)。前の設定を返す。"""
    global _config
    previous, _config = _config, (_validate(cfg) if cfg is not None else None)
    return previous


def as_date(when: When) -> date:
    return when.date() if isinstance(when, datetime) else when


def business_days(trade_date: date, expiry: date) -> int:
    """満期までの営業日数。取引日から満期日の前日まで(当日を1日と数える)。"""
    return CALENDAR.businessDaysBetween(ql.Date.from_date(trade_date), ql.Date.from_date(expiry), True, False)


def _unit_day(moment: datetime, cfg: TimeConfig) -> date:
    """moment がいる1営業日の当日(その営業日の15時までが、その営業日)。"""
    d = moment.date()
    if CALENDAR.isBusinessDay(ql.Date.from_date(d)) and moment.time() <= cfg.expiry_time:
        return d
    return CALENDAR.advance(ql.Date.from_date(d), 1, ql.Days).to_date()


def remaining_fraction(moment: datetime, cfg: Optional[TimeConfig] = None) -> float:
    """moment がいる1営業日の残りの割合(0〜1。前の営業日の15時直後は1、当日の15時に0)。"""
    cfg = cfg or config()
    day = _unit_day(moment, cfg)
    prev = CALENDAR.advance(ql.Date.from_date(day), -1, ql.Days).to_date()
    total = done = 0.0
    for start, end, weight in _windows(cfg, prev, day):
        total += weight
        done += weight * min(max((moment - start) / (end - start), 0.0), 1.0)
    return 1.0 - done / total


def remaining_days(when: When, expiry: date, cfg: Optional[TimeConfig] = None) -> float:
    """満期日の15時までの残り(営業日)。日付なら引け時点(= business_days)、時刻なら日中の残りも数える。
    満期を過ぎていれば0以下。"""
    cfg = cfg or config()
    if isinstance(when, datetime) and cfg.method == SESSION:
        return business_days(_unit_day(when, cfg), expiry) + remaining_fraction(when, cfg)
    return float(business_days(as_date(when), expiry))


def calendar_days(when: When, expiry: date, cfg: Optional[TimeConfig] = None) -> float:
    """満期日の15時までの暦日数(暦日ベースのvolの年率に使う)。日付なら満期日 − 取引日。"""
    cfg = cfg or config()
    if isinstance(when, datetime) and cfg.method == SESSION:
        return (datetime.combine(expiry, cfg.expiry_time) - when) / timedelta(days=1)
    return float((expiry - as_date(when)).days)


def option_term(when: When, expiry: date) -> float:
    """期間 T = 満期までの残りの営業日数 / 252(年)。満期を過ぎていればエラー。"""
    days = remaining_days(when, expiry)
    if days <= 0:
        raise ValueError(f"満期 {expiry} が取引日 {as_date(when)} 以前です")
    return days / TRADING_DAYS
