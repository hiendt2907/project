#!/usr/bin/env python3
"""Đo sức khoẻ đường chẩn đoán từ dữ liệu runtime THẬT — Đ52.

Sinh ra để trả lời đúng một câu, bằng số chứ không bằng cảm giác: *một evidence đi vào
thì người vận hành nhận được thứ gì có dùng được không?*

Vì sao cần một script riêng thay vì đọc log: audit Đ51 phải ghép tay 5 nguồn (Redis
session, trace stage, log pod, config LLM, collector trên VM) mới ra được con số, nên
không ai lặp lại được và không so sánh trước/sau được. Đây là cùng phép đo đó, đóng gói
để chạy lại bất cứ lúc nào.

Chạy:
    .venv/bin/python scripts/measure_diagnosis_health.py                 # toàn bộ
    .venv/bin/python scripts/measure_diagnosis_health.py --hours 2       # 2 giờ gần nhất
    .venv/bin/python scripts/measure_diagnosis_health.py --json out.json # lưu để so sau
    .venv/bin/python scripts/measure_diagnosis_health.py --compare a.json  # so với mốc cũ

Chỉ ĐỌC. Không sửa gì trong cluster.
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import subprocess
import sys
import time

NS = "multi-agent"
POD = "redis-0"

# Ngưỡng "hữu ích": có kết luận đủ tự tin VÀ lời khuyên gắn với host cụ thể.
# Hai điều kiện chứ không một — audit Đ51 cho thấy có ca confidence cao nhưng
# remediation vẫn rơi về generic fallback, và ca đó không giúp được ai.
USEFUL_MIN_CONFIDENCE = 0.7
GENERIC_MARKER = "generic fallback"


def _redis(*args: str) -> str:
    """Chạy redis-cli trong pod. Tách hàm để mọi lệnh đi qua đúng một đường."""
    out = subprocess.run(
        ["kubectl", "exec", "-n", NS, POD, "-c", "redis", "--", *args],
        capture_output=True, text=True, timeout=180,
    )
    if out.returncode != 0:
        raise SystemExit(f"redis-cli loi: {out.stderr[:300]}")
    return out.stdout


def _fetch_sessions() -> list[dict]:
    raw = _redis(
        "sh", "-c",
        'for k in $(redis-cli --scan --pattern "omni:diag:session:*"); '
        'do redis-cli GET "$k"; done',
    )
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # bản ghi hỏng không được làm chết cả phép đo
    return rows


def _fetch_latencies() -> list[float]:
    """Trễ EVIDENCE → DISPATCH (giây) — thời gian người vận hành thật sự phải chờ."""
    raw = _redis(
        "sh", "-c",
        'for k in $(redis-cli --scan --pattern "omni:trace:stages:ra-*"); do '
        'e=$(redis-cli HGET "$k" EVIDENCE); d=$(redis-cli HGET "$k" DISPATCH); '
        'echo "{\\"ev\\":${e:-null},\\"dp\\":${d:-null}}"; done',
    )
    lat = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
            ev, dp = o.get("ev") or {}, o.get("dp") or {}
            if ev.get("ts") and dp.get("ts"):
                lat.append(float(dp["ts"]) - float(ev["ts"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return lat


def measure(hours: float | None) -> dict:
    cutoff = (time.time() - hours * 3600) if hours else 0.0
    sessions = [s for s in _fetch_sessions() if (s.get("completed_at") or 0) >= cutoff]
    lat = sorted(_fetch_latencies())

    per_domain: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    hints: collections.Counter = collections.Counter()
    turns_total = turns_failed = 0

    for s in sessions:
        dom = s.get("domain") or "?"
        final = s.get("final") or {}
        try:
            conf = float(final.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        steps = " ".join(final.get("remediation_steps") or [])
        generic = GENERIC_MARKER in steps

        c = per_domain[dom]
        c["n"] += 1
        c["conf0"] += conf == 0.0
        c["generic"] += generic
        c["useful"] += (conf >= USEFUL_MIN_CONFIDENCE) and not generic
        hints[(s.get("alert_hint") or "").strip()] += 1

        for t in s.get("turns") or []:
            turns_total += 1
            turns_failed += str(t.get("hypothesis") or "") in ("llm_error", "parse_error")

    tot = collections.Counter()
    for c in per_domain.values():
        tot.update(c)

    n = tot["n"] or 1
    return {
        "measured_at": time.time(),
        "window_hours": hours,
        "sessions": tot["n"],
        "unique_alerts": len(hints),
        "repeat_ratio_pct": round(100 * (1 - len(hints) / n), 1),
        "useful_pct": round(100 * tot["useful"] / n, 1),
        "conf0_pct": round(100 * tot["conf0"] / n, 1),
        "generic_pct": round(100 * tot["generic"] / n, 1),
        "turns_total": turns_total,
        "turns_failed_pct": round(100 * turns_failed / (turns_total or 1), 1),
        "latency_median_s": round(statistics.median(lat), 1) if lat else None,
        "latency_p90_s": round(lat[int(len(lat) * 0.9) - 1], 1) if len(lat) > 1 else None,
        "latency_max_s": round(max(lat), 1) if lat else None,
        "per_domain": {d: dict(c) for d, c in sorted(
            per_domain.items(), key=lambda x: -x[1]["n"])},
        "top_alerts": [{"count": k, "hint": h[:110]} for h, k in hints.most_common(5)],
    }


def _fmt(m: dict) -> str:
    L = [
        f"cua so: {m['window_hours'] or 'tat ca'} gio | do luc "
        f"{time.strftime('%H:%M:%S', time.localtime(m['measured_at']))}",
        f"  session:            {m['sessions']}",
        f"  canh bao duy nhat:  {m['unique_alerts']}  (lap {m['repeat_ratio_pct']}%)",
        f"  HUU ICH:            {m['useful_pct']}%",
        f"  confidence = 0:     {m['conf0_pct']}%",
        f"  loi khuyen chung:   {m['generic_pct']}%",
        f"  luot LLM chet:      {m['turns_failed_pct']}%  ({m['turns_total']} luot)",
        f"  tre (trung vi/p90/max): {m['latency_median_s']}s / "
        f"{m['latency_p90_s']}s / {m['latency_max_s']}s",
        "  theo domain:",
    ]
    for d, c in m["per_domain"].items():
        L.append(f"    {d:<12} n={c['n']:<5} conf0={c['conf0']:<5} "
                 f"generic={c['generic']:<5} huu_ich={c['useful']}")
    return "\n".join(L)


def _delta(new: float | None, old: float | None, unit: str = "%") -> str:
    if new is None or old is None:
        return "n/a"
    d = new - old
    return f"{old}{unit} -> {new}{unit}  ({d:+.1f})"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hours", type=float, default=None)
    p.add_argument("--json", help="ghi ket qua ra file de so sanh ve sau")
    p.add_argument("--compare", help="file moc cu de so sanh")
    a = p.parse_args()

    m = measure(a.hours)
    print(_fmt(m))

    if a.json:
        with open(a.json, "w") as f:
            json.dump(m, f, indent=1)
        print(f"\n[da luu moc: {a.json}]")

    if a.compare:
        try:
            old = json.load(open(a.compare))
        except OSError as exc:
            raise SystemExit(f"khong doc duoc moc cu: {exc}")
        print(f"\n=== SO VOI {a.compare} ===")
        print(f"  HUU ICH:          {_delta(m['useful_pct'], old['useful_pct'])}")
        print(f"  confidence = 0:   {_delta(m['conf0_pct'], old['conf0_pct'])}")
        print(f"  loi khuyen chung: {_delta(m['generic_pct'], old['generic_pct'])}")
        print(f"  luot LLM chet:    {_delta(m['turns_failed_pct'], old['turns_failed_pct'])}")
        print(f"  tre trung vi:     {_delta(m['latency_median_s'], old['latency_median_s'], 's')}")
        print(f"  so session:       {old['sessions']} -> {m['sessions']}")


if __name__ == "__main__":
    sys.exit(main())
