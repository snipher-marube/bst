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
BRAND_COLORS = ['#1f77b4', '#f2a900', '#10a37f', '#3e8ec6',
                '#f6b93b', '#2fb58f', '#165a8a', '#0d7a60']
CHART_BG = '#dbe5f1'
CHART_GRID = '#ffffff'


def _to_image(fig, dpi: int = 150) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=dpi, bbox_inches='tight', facecolor=CHART_BG)
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
    c = color or '#4f8fdd'
    ax.set_facecolor(CHART_BG)
    fig.patch.set_facecolor(CHART_BG)
    ax.plot(labels, values, color=c, linewidth=2, marker='o', markersize=4)
    ax.fill_between(range(len(labels)), values, alpha=0.12, color='#4f8fdd')
    ax.set_title(title, fontsize=11, fontweight='bold', pad=8)
    ax.set_ylabel(y_label, fontsize=9)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda x, _: f'{x:,.0f}' if x == int(x) else f'{x:,.2f}'
    ))
    ax.tick_params(axis='x', labelsize=7, rotation=30)
    ax.tick_params(axis='y', labelsize=7)
    ax.grid(axis='y', color=CHART_GRID, linewidth=0.8, alpha=0.82)
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
    ax.set_facecolor(CHART_BG)
    fig.patch.set_facecolor(CHART_BG)
    pairs = sorted(zip(labels, values), key=lambda item: item[1], reverse=True)
    labels = [label for label, _ in pairs]
    values = [value for _, value in pairs]
    idx = range(len(labels))
    if horizontal:
        split_index = max(1, len(labels) // 2 + (len(labels) % 2))
        colors = [
            ['#19d3b5', '#12cbb2', '#0fc2ad', '#0db8a6'][i % 4] if i < split_index
            else ['#4f8fdd', '#4384d2', '#3779c7', '#2c6fbc'][(i - split_index) % 4]
            for i in range(len(labels))
        ]
        ax.barh(idx, values, color=colors, height=0.5)
        ax.set_yticks(list(idx))
        ax.set_yticklabels([str(l) for l in labels], fontsize=7)
        ax.invert_yaxis()
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(
            lambda x, _: f'{x:,.0f}' if x == int(x) else f'{x:,.2f}'
        ))
    else:
        colors = [BRAND_COLORS[i % len(BRAND_COLORS)] for i in range(len(labels))]
        ax.bar(idx, values, color=colors, width=0.48)
        ax.set_xticks(list(idx))
        ax.set_xticklabels([str(l) for l in labels], rotation=45, ha='right', fontsize=7)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(
            lambda x, _: f'{x:,.0f}' if x == int(x) else f'{x:,.2f}'
        ))
    ax.grid(axis='x' if horizontal else 'y', color=CHART_GRID, linewidth=0.8, alpha=0.82)
    ax.set_axisbelow(True)
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
    ax.set_facecolor(CHART_BG)
    fig.patch.set_facecolor(CHART_BG)
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
    Pull labels/values from a widget query result.

    The QueryEngine returns charts as:
        {'val': {'Jan': 100, 'Feb': 200, ...}}   ← grouped dict under agg name
    It may also return:
        {'labels': [...], 'values': [...]}        ← explicit lists
        [{'category': 'A', 'val': 30}, ...]       ← list of row dicts
    """
    if not widget_data:
        return [], []

    if isinstance(widget_data, dict):
        # Explicit lists format
        if 'labels' in widget_data and 'values' in widget_data:
            return widget_data['labels'], widget_data['values']

        # Recurse into 'data' wrapper
        if 'data' in widget_data and isinstance(widget_data['data'], dict):
            return extract_chart_data(widget_data['data'])

        # QueryEngine grouped-dict format: {'val': {'Jan': 100, ...}, ...}
        # Find the first key whose value is itself a dict (label→number map)
        for key in ('val', 'value', 'count'):
            inner = widget_data.get(key)
            if isinstance(inner, dict) and inner:
                labels, values = [], []
                for lbl, v in inner.items():
                    labels.append(str(lbl))
                    try:
                        values.append(float(v))
                    except (TypeError, ValueError):
                        values.append(0.0)
                return labels, values

        # Fallback: any value that is a non-empty dict
        for v in widget_data.values():
            if isinstance(v, dict) and v:
                labels, values = [], []
                for lbl, num in v.items():
                    labels.append(str(lbl))
                    try:
                        values.append(float(num))
                    except (TypeError, ValueError):
                        values.append(0.0)
                return labels, values

    if isinstance(widget_data, list) and widget_data:
        first = widget_data[0]
        if isinstance(first, dict):
            val_keys = [k for k in ('val', 'value', 'count') if k in first]
            lbl_keys = [k for k in first if k not in ('val', 'value', 'count')]
            if lbl_keys and val_keys:
                lk, vk = lbl_keys[0], val_keys[0]
                labels = [str(row.get(lk, '')) for row in widget_data]
                values = []
                for row in widget_data:
                    try:
                        values.append(float(row.get(vk, 0) or 0))
                    except (TypeError, ValueError):
                        values.append(0.0)
                return labels, values

    return [], []
