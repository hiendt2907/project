"""Matplotlib → PNG bytes trong RAM (BytesIO). Không ghi /tmp hay đĩa."""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def line_chart_png_bytes(
    x: Sequence[float] | Sequence[int],
    y: Sequence[float] | Sequence[int],
    *,
    title: str,
    xlabel: str = "",
    ylabel: str = "",
    figsize: tuple[float, float] = (9.0, 4.5),
    dpi: int = 110,
) -> bytes:
    """
    Vẽ biểu đồ đường, trả về bytes PNG. Buffer chỉ tồn tại trong RAM;
    figure đóng sau khi render để tránh rò rỉ bộ nhớ matplotlib.
    """
    if len(x) != len(y):
        raise ValueError("x và y phải cùng độ dài")
    if len(x) == 0:
        raise ValueError("cần ít nhất một điểm dữ liệu")

    fig, ax = plt.subplots(figsize=figsize, layout="constrained")
    try:
        ax.plot(x, y, linewidth=1.5)
        ax.set_title(title)
        if xlabel:
            ax.set_xlabel(xlabel)
        if ylabel:
            ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)

        buf = io.BytesIO()
        try:
            fig.savefig(buf, format="png", dpi=dpi)
            return buf.getvalue()
        finally:
            buf.close()
    finally:
        plt.close(fig)


def line_chart_history_forecast_png_bytes(
    x_hist: Sequence[float] | Sequence[int],
    y_hist: Sequence[float] | Sequence[int],
    x_fore: Sequence[float] | Sequence[int] | None,
    y_fore: Sequence[float] | Sequence[int] | None,
    *,
    title: str,
    xlabel: str = "",
    ylabel: str = "",
    figsize: tuple[float, float] = (9.0, 4.5),
    dpi: int = 110,
) -> bytes:
    """Lịch sử (nét liền) + dự đoán (nét đứt, cùng figure)."""
    if len(x_hist) != len(y_hist):
        raise ValueError("x_hist và y_hist phải cùng độ dài")
    if len(x_hist) == 0:
        raise ValueError("cần ít nhất một điểm lịch sử")
    if (x_fore is None) ^ (y_fore is None):
        raise ValueError("x_fore và y_fore cùng có hoặc cùng không")
    if x_fore is not None and y_fore is not None and len(x_fore) != len(y_fore):
        raise ValueError("x_fore và y_fore phải cùng độ dài")

    fig, ax = plt.subplots(figsize=figsize, layout="constrained")
    try:
        ax.plot(x_hist, y_hist, linewidth=1.5, label="history")
        if x_fore is not None and y_fore is not None and len(x_fore) > 0:
            ax.plot(x_fore, y_fore, linewidth=1.5, linestyle="--", label="forecast")
        ax.legend(loc="best")
        ax.set_title(title)
        if xlabel:
            ax.set_xlabel(xlabel)
        if ylabel:
            ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)

        buf = io.BytesIO()
        try:
            fig.savefig(buf, format="png", dpi=dpi)
            return buf.getvalue()
        finally:
            buf.close()
    finally:
        plt.close(fig)


def line_chart_history_forecast_ci_png_bytes(
    hist_x: Sequence[float] | Sequence[Any],
    hist_y: Sequence[float] | Sequence[int],
    fore_x: Sequence[float] | Sequence[Any],
    fore_yhat: Sequence[float],
    fore_lower: Sequence[float],
    fore_upper: Sequence[float],
    *,
    title: str,
    figsize: tuple[float, float] = (9.0, 4.5),
    dpi: int = 110,
) -> bytes:
    """
    Lịch sử (đen, nét chấm) + dự báo (xanh, nét đứt) + vùng tin cậy ``fill_between``.
    Trục X/Y: Thời gian / Giá trị (tiếng Việt). Không ghi đĩa — chỉ bytes PNG.
    """
    if len(hist_x) != len(hist_y):
        raise ValueError("hist_x và hist_y phải cùng đội dài")
    if len(hist_x) == 0:
        raise ValueError("cần ít nhất một điểm lịch sử")
    n = len(fore_x)
    if not (n == len(fore_yhat) == len(fore_lower) == len(fore_upper)):
        raise ValueError("future series phải cùng đội dài")
    fig, ax = plt.subplots(figsize=figsize, layout="constrained")
    try:
        ax.plot(
            hist_x,
            hist_y,
            color="black",
            linestyle=":",
            linewidth=1.6,
            marker="o",
            markersize=2.5,
            label="Thực tế",
        )
        if n > 0:
            ax.fill_between(fore_x, fore_lower, fore_upper, color="#2ecc71", alpha=0.25, label="Khoảng tin cậy")
            ax.plot(
                fore_x,
                fore_yhat,
                color="#27ae60",
                linestyle="--",
                linewidth=1.6,
                label="Dự báo",
            )
        ax.set_title(title)
        ax.set_xlabel("Thời gian")
        ax.set_ylabel("Giá trị")
        ax.grid(True, alpha=0.35)
        ax.legend(loc="best")

        buf = io.BytesIO()
        try:
            fig.savefig(buf, format="png", dpi=dpi)
            return buf.getvalue()
        finally:
            buf.close()
    finally:
        plt.close(fig)


def pod_cpu_memory_bar_png_bytes(
    cpu_pct: float,
    mem_pct: float,
    *,
    title: str,
    figsize: tuple[float, float] = (4.2, 2.4),
    dpi: int = 110,
) -> bytes:
    """Thanh ngang % CPU / RAM (0–100) — mini chart cho inspect Pod."""
    cpu_c = min(max(float(cpu_pct), 0.0), 100.0)
    mem_c = min(max(float(mem_pct), 0.0), 100.0)
    fig, ax = plt.subplots(figsize=figsize, layout="constrained")
    try:
        labels = [f"CPU {cpu_c:.0f}%", f"RAM {mem_c:.0f}%"]
        ax.barh(labels, [cpu_c, mem_c], color=["#27ae60", "#2980b9"], height=0.45)
        ax.set_xlim(0, 100)
        ax.set_xlabel("% so với limit (hoặc request)")
        ax.set_title(title[:80], fontsize=10)
        ax.grid(True, axis="x", alpha=0.3)
        buf = io.BytesIO()
        try:
            fig.savefig(buf, format="png", dpi=dpi)
            return buf.getvalue()
        finally:
            buf.close()
    finally:
        plt.close(fig)


def pod_cpu_memory_usage_absolute_png_bytes(
    *,
    use_cpu_cores: float,
    use_mem_bytes: int,
    title: str,
    figsize: tuple[float, float] = (4.2, 3.0),
    dpi: int = 110,
) -> bytes:
    """Khi pod không khai báo limit/request: hiển thị usage thực (cores + GiB), không ép % limit=0."""
    mem_gib = max(float(use_mem_bytes) / (1024**3), 0.0)
    cpu_v = max(float(use_cpu_cores), 0.0)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=figsize, layout="constrained")
    try:
        ax1.barh(["CPU"], [cpu_v], color="#27ae60", height=0.35)
        ax1.set_xlabel("cores (metrics.k8s.io)")
        ax1.set_title(title[:80], fontsize=10)
        ax1.grid(True, axis="x", alpha=0.3)
        ax2.barh(["RAM"], [mem_gib], color="#2980b9", height=0.35)
        ax2.set_xlabel("GiB (working set)")
        ax2.grid(True, axis="x", alpha=0.3)
        buf = io.BytesIO()
        try:
            fig.savefig(buf, format="png", dpi=dpi)
            return buf.getvalue()
        finally:
            buf.close()
    finally:
        plt.close(fig)
