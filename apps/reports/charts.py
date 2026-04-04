"""
Server-side chart rendering with matplotlib.

Each function receives already-fetched widget data and returns a PNG bytes
object suitable for embedding directly into a ReportLab PDF.
"""
import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Brand palette — matches the Plotly frontend colours
BRAND_COLORS = ['#03466e', '#0a8043', '#e37400', '#8e24aa',
                '#1a73e8', '#c62828', '#00838f', '#f4511e']


def _to_image(fig, dpi: int = 150) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=dpi, bbox_inches='tight', facecolor='white')
    buf.seek(0)
    return buf.read()


def _get_matplotlib():
    """Lazy import so the module still loads even if matplotlib isn't installed."""
    try:
        import matplotlib
        matplotlib.use('Agg')          # non-interactive backend
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mticker
        return plt, mticker
    except ImportError as exc:
        raise RuntimeError(
            "matplotlib is required for PDF chart rendering. "
            "Add 'matplotlib' to requirements.txt and reinstall."
        ) from exc


def render_line_chart(
    title: str,
    labels: list,
    values: list,
    y_label: str = '',
    color: str | None = None,
    width_in: float = 6.0,
    height_in: float = 3.0,
) -> bytes:
    plt, mticker = _get_matplotlib()
    fig, ax = plt.subplots(figsize=(width_in, height_in))
    c = color or BRAND_COLORS[0]
    ax.plot(labels, values, color=c, linewidth=2, marker='o', markersize=4)
    ax.fill_between(range(len(labels)), values, alpha=0.12, color=c)
    ax.set_title(title, fontsize=11, fontweight='bold', pad=8)
    ax.set_ylabel(y_label, fontsize=9)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda x, _: f'{x:,.0f}' if x == int(x) else f'{x:,.2f}'
    ))
    ax.tick_params(axis='x', labelsize=7, rotation=30)
    ax.tick_params(axis='y', labelsize=7)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels([str(l) for l in labels], rotation=30, ha='right')
    fig.tight_layout()
    data = _to_image(fig)
    plt.close(fig)
    return data


def render_bar_chart(
    title: str,
    labels: list,
    values: list,
    y_label: str = '',
    horizontal: bool = False,
    width_in: float = 6.0,
    height_in: float = 3.0,
) -> bytes:
    plt, mticker = _get_matplotlib()
    fig, ax = plt.subplots(figsize=(width_in, height_in))
    colors = [BRAND_COLORS[i % len(BRAND_COLORS)] for i in range(len(labels))]
    idx = range(len(labels))
    if horizontal:
        ax.barh(idx, values, color=colors)
        ax.set_yticks(list(idx))
        ax.set_yticklabels([str(l) for l in labels], fontsize=7)
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(
            lambda x, _: f'{x:,.0f}' if x == int(x) else f'{x:,.2f}'
        ))
    else:
        ax.bar(idx, values, color=colors)
        ax.set_xticks(list(idx))
        ax.set_xticklabels([str(l) for l in labels], rotation=30, ha='right', fontsize=7)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(
            lambda x, _: f'{x:,.0f}' if x == int(x) else f'{x:,.2f}'
        ))
    ax.set_title(title, fontsize=11, fontweight='bold', pad=8)
    ax.set_ylabel(y_label if not horizontal else '', fontsize=9)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    fig.tight_layout()
    data = _to_image(fig)
    plt.close(fig)
    return data


def render_pie_chart(
    title: str,
    labels: list,
    values: list,
    width_in: float = 4.5,
    height_in: float = 3.5,
) -> bytes:
    plt, _ = _get_matplotlib()
    fig, ax = plt.subplots(figsize=(width_in, height_in))
    colors = [BRAND_COLORS[i % len(BRAND_COLORS)] for i in range(len(labels))]
    wedges, texts, autotexts = ax.pie(
        values, labels=None, autopct='%1.1f%%',
        colors=colors, startangle=90,
        wedgeprops={'edgecolor': 'white', 'linewidth': 1},
        pctdistance=0.82,
    )
    for at in autotexts:
        at.set_fontsize(7)
    ax.legend(
        wedges, [str(l) for l in labels],
        loc='lower center', bbox_to_anchor=(0.5, -0.2),
        ncol=min(4, len(labels)), fontsize=7, frameon=False,
    )
    ax.set_title(title, fontsize=11, fontweight='bold', pad=8)
    fig.tight_layout()
    data = _to_image(fig)
    plt.close(fig)
    return data


def render_kpi_card(
    title: str,
    value: Any,
    prefix: str = '',
    suffix: str = '',
    width_in: float = 2.8,
    height_in: float = 1.6,
    color: str | None = None,
) -> bytes:
    plt, _ = _get_matplotlib()
    c = color or BRAND_COLORS[0]
    fig, ax = plt.subplots(figsize=(width_in, height_in))
    ax.set_facecolor(c)
    fig.patch.set_facecolor(c)
    ax.axis('off')

    # Format value
    try:
        v = float(value)
        if v >= 1_000_000:
            disp = f'{prefix}{v/1_000_000:.1f}M{suffix}'
        elif v >= 1_000:
            disp = f'{prefix}{v/1_000:.1f}K{suffix}'
        else:
            disp = f'{prefix}{v:,.2f}{suffix}' if v != int(v) else f'{prefix}{int(v):,}{suffix}'
    except (TypeError, ValueError):
        disp = str(value)

    ax.text(0.5, 0.62, disp, transform=ax.transAxes,
            fontsize=22, fontweight='bold', color='white',
            ha='center', va='center')
    ax.text(0.5, 0.22, title, transform=ax.transAxes,
            fontsize=9, color=(1, 1, 1, 0.85),
            ha='center', va='center')
    fig.tight_layout(pad=0.3)
    data = _to_image(fig, dpi=120)
    plt.close(fig)
    return data


def extract_chart_data(widget_data: dict) -> tuple[list, list]:
    """
    Pull labels/values from the widget query result dict.
    Handles {'labels': [...], 'values': [...]} and [{'label':..., 'value':...}] formats.
    Returns (labels, values) — both lists, possibly empty.
    """
    if not widget_data:
        return [], []
    if isinstance(widget_data, dict):
        if 'labels' in widget_data and 'values' in widget_data:
            return widget_data['labels'], widget_data['values']
        if 'data' in widget_data:
            return extract_chart_data(widget_data['data'])
        # flat dict → single-item
        return list(widget_data.keys()), list(widget_data.values())
    if isinstance(widget_data, list) and widget_data:
        first = widget_data[0]
        if isinstance(first, dict):
            key_candidates = [k for k in first if k not in ('val', 'value', 'count')]
            val_candidates = [k for k in ('val', 'value', 'count') if k in first]
            if key_candidates and val_candidates:
                lk = key_candidates[0]
                vk = val_candidates[0]
                labels = [str(row.get(lk, '')) for row in widget_data]
                values = []
                for row in widget_data:
                    try:
                        values.append(float(row.get(vk, 0) or 0))
                    except (TypeError, ValueError):
                        values.append(0.0)
                return labels, values
    return [], []
