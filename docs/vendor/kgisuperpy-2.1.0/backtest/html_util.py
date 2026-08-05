from __future__ import annotations
"""Dashboard HTML Generation Utilities
儀表板 HTML 生成工具

Provides functions to generate HTML components for backtesting dashboards including:
- Metrics cards and KPI displays
- Interactive Plotly charts (pie charts, heatmaps, candlestick charts)
- Paginated data tables with filters
- CSV export functionality

提供用於回測儀表板的 HTML 組件生成功能，包括：
- 指標卡片和 KPI 顯示
- 互動式 Plotly 圖表（餅圖、熱圖、K線圖）
- 帶篩選器的分頁數據表
- CSV 匯出功能

Security: All user-provided data is HTML-escaped to prevent XSS attacks.
安全性：所有用戶提供的數據都經過 HTML 轉義以防止 XSS 攻擊。
"""

from pathlib import Path
import pandas as pd
import numpy as np
from plotly.io import to_html
from dash import dcc, html
import plotly.graph_objs as go
import plotly.io as pio
import json
import html as _html

# ============================================================================
# Constants
# 常數定義
# ============================================================================
DEFAULT_PAGE_SIZE = 20
DEFAULT_PIE_CHART_SIZE = 350  # pixels
DEFAULT_FIGURE_HEIGHT = 400  # pixels
CANDLESTICK_CHART_HEIGHT = 600  # pixels

# ============================================================================
# Metrics Generation Functions
# 指標生成函數
# ============================================================================

def generate_metrics_html(metrics: list[dict[str, str]] | None = None) -> str:
    """
    Generate HTML for top-level metrics display cards.
    生成頂層指標顯示卡片的 HTML。
    
    Args:
        metrics: List of dicts with 'value' and 'label' keys, e.g.
                 [{'value': '12.34%', 'label': '總報酬率'}, ...]
                 If None, returns placeholder demo metrics.
    
    Returns:
        HTML string with metric cards
    """
    if metrics is None:
        metrics = [
        {"value": "12.34%", "label": "總報酬率"},
        {"value": "8.90%",  "label": "年化報酬率"},
        {"value": "1.45",   "label": "Sharpe比率"},
        {"value": "56.00%", "label": "勝率"},
        {"value": "-12.88%", "label": "最大回撤"},
        {"value": "45",     "label": "最大回撤天數"},
        {"value": "1.23",   "label": "Sortino比率"},
        {"value": "1.80",   "label": "盈虧比"},
    ]
    metrics_html = "\n".join(
        f"""
        <div class="featureBox-item">
            <p class="text">{_html.escape(str(m['value']))}</p>
            <p class="name">{_html.escape(str(m['label']))}</p>
        </div>
        """
        for m in metrics
    )
    return metrics_html


def generate_return_metrics_html(return_metrics: list[dict[str, str | float]] | None = None) -> str:
    """
    Generate HTML for return metrics with color coding (positive=red, negative=green).
    生成帶顏色編碼的回報指標 HTML（正值=紅色，負值=綠色）。
    
    Args:
        return_metrics: List of dicts with 'label' and 'value' keys:
            [{'label': '周報酬率', 'value': 10}, ...]
            If None, returns placeholder demo metrics.
    
    Returns:
        HTML string with colored return metric cards
    """
    if return_metrics is None:
        return_metrics = [
            {"label": "周報酬率", "value": 10},
            {"label": "月報酬率", "value": -8},
            {"label": "季報酬率", "value": 6},
            {"label": "年報酬率", "value": 12},
        ]

    items = []
    for m in return_metrics:
        # Extract numeric value for color determination
        # 提取數值以確定顏色
        numeric_value = m["value"]

        try:
            val = float(numeric_value)
            # Positive returns = red, negative returns = green (Taiwan stock market convention)
            # 正報酬 = 紅色，負報酬 = 綠色（台灣股市慣例）
            color = "#ff4444" if val > 0 else "#33cc33"
        except ValueError:
            # Default neutral color if value cannot be converted to float
            # 如果值無法轉換為浮點數，使用預設中性顏色
            color = "#263348"

        card_html = f"""
        <div class="featureBox-item">
            <p class="name">{_html.escape(str(m['label']))}</p>
            <p class="text" style="color:{color};">{_html.escape(str(m['value']))}%</p>
        </div>
        """
        items.append(card_html)

    return "\n".join(items)

def generate_additional_metrics_html(additional_metrics: list[dict[str, str]] | None = None) -> str:
    """
    Generate HTML for additional metrics display.
    生成額外指標顯示的 HTML。
    
    Args:
        additional_metrics: List of dicts with 'label' and 'value' keys
        
    Returns:
        HTML string with additional metric cards
    """
    if additional_metrics is None:
        additional_metrics = [
            {"label": "交易次數", "value": "150"},
            {"label": "平均持有天數", "value": "20"},
            {"label": "最大單筆獲利", "value": "8.5%"},
            {"label": "最大單筆虧損", "value": "-5.2%"},
        ]

    items = []
    for m in additional_metrics:
        card_html = f"""
        <div class="featureBox-item">
            <p class="name">{_html.escape(str(m['label']))}:</p>
            <h4 class="text">{_html.escape(str(m['value']))}</h4>
        </div>
        """
        items.append(card_html)

    return "\n".join(items)

# ============================================================================
# Table Generation Functions
# 表格生成函數
# ============================================================================

def create_styled_table(
    df: pd.DataFrame,
    table_id: str = "styled_table",
    left_align_first_col: bool = True
) -> str:
    """
    Generate HTML table from pandas DataFrame with proper escaping.
    從 pandas DataFrame 生成帶適當轉義的 HTML 表格。
    
    Args:
        df: Source DataFrame
        table_id: HTML id attribute for the table element
        left_align_first_col: If True, first column is left-aligned
        
    Returns:
        HTML string with complete table markup
        
    Raises:
        ValueError: If DataFrame is empty
    """
    if df is None or df.empty:
        raise ValueError("Cannot create table from empty DataFrame")
    
    # Create a copy to avoid modifying the original DataFrame
    # 創建副本以避免修改原始 DataFrame
    df = df.copy()

    # Convert datetime columns to string format for display
    # 將日期時間列轉換為字符串格式以便顯示
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime("%Y-%m-%d")

    # Generate table header with escaped column names
    # 生成帶轉義列名的表頭
    header_html = "".join(f"<th>{_html.escape(str(col))}</th>" for col in df.columns)

    # Generate table body rows with HTML escaping for security
    # 生成帶 HTML 轉義的表格主體行以確保安全性
    body_rows = []
    for _, row in df.iterrows():
        tds = []
        for idx, col in enumerate(df.columns):
            cell_value = _html.escape(str(row[col]))
            # First column gets left alignment class
            # 第一列使用左對齊樣式
            if left_align_first_col and idx == 0:
                tds.append(f'<td class="tL">{cell_value}</td>')
            else:
                tds.append(f"<td>{cell_value}</td>")
        body_rows.append("<tr>" + "".join(tds) + "</tr>")

    body_html = "\n".join(body_rows)

    return f"""
    <table id="{table_id}" class="static-table">
        <thead><tr>{header_html}</tr></thead>
        <tbody>{body_html}</tbody>
    </table>
    """



def generate_remainder_table_html(remainder_df: pd.DataFrame | None = None, table_id: str = "remainder_table") -> str:
    """
    Generate remainder/summary table HTML.
    生成剩餘/摘要表格 HTML。
    
    Args:
        remainder_df: DataFrame with summary data. If None, uses placeholder data.
        table_id: HTML id for table element

    Returns:
        HTML string with summary table
    """
    if remainder_df is None:
        
        data = {
            "資料明細": [
                "第一筆交易的日期",
                "最新資料日期",
                "回測執行日期",
                "最大倉位所需資金",
                "淨利潤",
                "交易費用",
                "平均交易盈虧",
            ],
            "數值": [
                "2021-12-27",
                "2025-11-14",
                "2025-11-16",
                "3040201",
                "1644562",
                "93240",
                "29901.00",
            ],
        }
        remainder_df = pd.DataFrame(data)
    
    remainder_html = create_styled_table(remainder_df, table_id=table_id)
    return remainder_html   

# ============================================================================
# Chart Generation Functions  
# 圖表生成函數
# ============================================================================

def create_pie_chart_html(avg_cash_utilization_rate: float = 0.75) -> str:
    """
    Generate HTML for pie chart showing cash utilization rate.
    生成顯示現金使用率的餅圖 HTML。
    
    Args:
        avg_cash_utilization_rate: Utilization rate as decimal (0.0-1.0)
        
    Returns:
        HTML string with embedded Plotly pie chart
    """
    pie_chart = create_pie_chart(avg_cash_utilization_rate)
    pie_chart_inner = to_html(
        pie_chart.figure,
        include_plotlyjs=False,
        full_html=False,
        default_width=f"{DEFAULT_PIE_CHART_SIZE}px",
        default_height=f"{DEFAULT_PIE_CHART_SIZE}px",
    )
    pie_chart_html = f"""
    <div style="width:{DEFAULT_PIE_CHART_SIZE}px;height:{DEFAULT_PIE_CHART_SIZE}px;margin:0 auto;">
        {pie_chart_inner}
    </div>
    """
    return pie_chart_html

def create_pie_chart(avg_cash_utilization_rate: float) -> dcc.Graph:
    """
    Create pie chart Graph object for cash utilization.
    創建現金使用率餅圖物件。
    
    Args:
        avg_cash_utilization_rate: Utilization rate as decimal (0.0-1.0)
        
    Returns:
        Dash Graph component with pie chart
    """
    return dcc.Graph(
        id="pie-chart",
        figure=go.Figure(
            data=[
                go.Pie(
                    values=[
                        round(
                            avg_cash_utilization_rate * 100,
                            2,
                        ),
                        round(
                            100
                            - avg_cash_utilization_rate * 100,
                            2,
                        ),
                    ],
                    hole=0.7,
                    labels=["已使用", "未使用"],
                    marker_colors=[
                        "rgb(97, 123, 146, 1)",  # Darker blue shade
                        "rgba(59, 130, 246, 0.2)",  # Lighter blue with some transparency for gradient effect
                    ],
                )
            ],
            layout=go.Layout(
                annotations=[
                    {
                        "text": f'{round(avg_cash_utilization_rate * 100, 2)}%<br>資金使用率',
                        "x": 0.5,
                        "y": 0.5,
                        "showarrow": False,
                        "font": {"size": 20},
                    }
                ],
                margin=dict(t=0, b=0, l=0, r=0),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
            ),
        ),
        config={"responsive": True},
        style={"height": f"{DEFAULT_PIE_CHART_SIZE}px", "width": f"{DEFAULT_PIE_CHART_SIZE}px"},
    )


def generate_heatmap_html(heatmap_fig: str | go.Figure | None = None) -> str:
    """
    Generate HTML for heatmap figure.
    生成熱圖 HTML。
    
    Args:
        heatmap_fig: Either a filename (str) or Plotly Figure object
        
    Returns:
        HTML string with heatmap, or empty string if None
    """
    if heatmap_fig is None:
        return ""

    # Load figure from JSON file if string path provided
    # 如果提供字符串路徑，則從 JSON 文件加載圖表
    if isinstance(heatmap_fig, str):
        fig_path = Path(__file__).parent.parent / "dashboard_data" / "fig_data" / heatmap_fig
        if not fig_path.exists():
            raise FileNotFoundError(f"Heatmap figure file not found: {fig_path}")
        heatmap_fig = pio.read_json(fig_path)
        
    # Convert Plotly figure to HTML without including plotly.js (loaded globally)
    # 將 Plotly 圖表轉換為 HTML，不包含 plotly.js（全局加載）
    heatmap_html = heatmap_fig.to_html(
        include_plotlyjs=False,
        full_html=False,
        div_id="heatmap-container",
    )
    return heatmap_html


def generate_single_figure_html(fig: str | go.Figure, div_id: str) -> str:
    """
    Generate HTML for a single Plotly figure.
    生成單個 Plotly 圖表的 HTML。
    
    Args:
        fig: Either a filename (str) or Plotly Figure object
        div_id: HTML div id for the figure container
        
    Returns:
        HTML string with embedded figure
        
    Raises:
        FileNotFoundError: If figure file doesn't exist
    """
    if isinstance(fig, str):
        fig_json_path = Path(__file__).parent.parent / "dashboard_data" / "fig_data" / fig
        if not fig_json_path.exists():
            raise FileNotFoundError(f"Figure file not found: {fig_json_path}")
        fig = pio.read_json(fig_json_path)

    # Make Plotly responsive and use 100% width
    fig.update_layout(autosize=True)

    return fig.to_html(
        include_plotlyjs=False,
        full_html=False,
        div_id=div_id,
        config={"responsive": True},
        default_width="100%",
        default_height=f"{DEFAULT_FIGURE_HEIGHT}px",
    )




def generate_dd_html(df: pd.DataFrame = None) -> str:
    """
    Generate the complete max 5 drawdown section with title, export button, and table.
    生成最大5個回撤區段（包含標題、匯出按鈕和表格）
    """
    base_table = create_styled_table(df, table_id="max5_drawdown_table")
    wrapped_table = f'<div class="table-scroll-wrapper">\n{base_table}\n</div>'
    return generate_table_section_html(
        table_html=wrapped_table,
        title="最大5個回撤",
        table_id="max5_drawdown_table",
        section_id="max5-dd-row",
        export_visible=True
    )


def generate_trade_table_html(df: pd.DataFrame, page_size: int = DEFAULT_PAGE_SIZE) -> str:
    """
    Generate the complete trade table section with title, export button, and paginated table.
    生成完整的交易紀錄區段（包含標題、匯出按鈕和分頁表格）
    
    Args:
        df: Trade DataFrame
        page_size: Rows per page for pagination (default: 20)
    
    Returns:
        Complete HTML section with pagination
    """
    base_table = create_styled_table(df, table_id="trade_table")
    paginated_table = wrap_table_with_pagination(
        base_table,
        table_id="trade_table",
        page_size=page_size
    )
    return generate_table_section_html(
        table_html=paginated_table,
        title="交易紀錄",
        table_id="trade_table",
        section_id="trade",
        export_visible=True
    )


def generate_daily_table_section_html(
    df: pd.DataFrame,
    page_size: int = DEFAULT_PAGE_SIZE,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """
    Generate the complete daily table section with title, export button, date filters, and paginated table.
    生成完整的每日摘要區段（包含標題、匯出按鈕、日期篩選器和分頁表格）
    
    Args:
        df: Daily summary DataFrame
        page_size: Rows per page for pagination
        start_date: Default start date for filter (YYYY-MM-DD format)
        end_date: Default end date for filter (YYYY-MM-DD format)
    
    Returns:
        Complete HTML section with all components
    """
    base_table = create_styled_table(df, table_id="daily_table")
    paginated_table = wrap_table_with_pagination(
        base_table,
        table_id="daily_table",
        page_size=page_size,
        start_date=start_date,
        end_date=end_date
    )
    
    return generate_table_section_with_date_filters_html(
        table_html=paginated_table,
        title="每日摘要",
        table_id="daily_table",
        section_id="daily",
        export_visible=True,
        start_input_id="daily-start",
        end_input_id="daily-end"
    )


def generate_position_table_section_html(
    df: pd.DataFrame,
    page_size: int = 20,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """
    Generate the complete position table section with title, export button, filters, and paginated table.
    生成完整的部位明細區段（包含標題、匯出按鈕、篩選器和分頁表格）
    
    Args:
        df: Position detail DataFrame (must have '股票代號' column)
        page_size: Rows per page for pagination
        start_date: Default start date for filter (YYYY-MM-DD format)
        end_date: Default end date for filter (YYYY-MM-DD format)
    
    Returns:
        Complete HTML section with all components
    """
    # Auto-generate position options from DataFrame
    stock_codes = sorted(df["股票代號"].astype(str).unique())
    position_options_html = "\n".join(f'<option value="{x}">{x}</option>' for x in stock_codes)
    
    base_table = create_styled_table(df, table_id="position_table")
    paginated_table = wrap_table_with_pagination(
        base_table,
        table_id="position_table",
        page_size=page_size,
        start_date=start_date,
        end_date=end_date
    )
    
    return generate_table_section_with_position_filters_html(
        table_html=paginated_table,
        title="部位明細",
        table_id="position_table",
        position_options_html=position_options_html,
        section_id="position",
        export_visible=True,
        symbol_select_id="position-symbol",
        start_input_id="position-start",
        end_input_id="position-end"
    )


def wrap_table_with_pagination(
    table_html: str,
    table_id: str,
    page_size: int = DEFAULT_PAGE_SIZE,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """
    Wrap a static table in a pagination container.
    將靜態表格包裝在分頁容器中。
    
    Pagination is aware of filters via row.dataset.visible ('1' or '0').
    分頁通過 row.dataset.visible ('1' 或 '0') 感知篩選器。
    
    Args:
        table_html: The base table HTML
        table_id: ID of the table element
        page_size: Number of rows per page
        start_date: Default start date for date filters
        end_date: Default end date for date filters
        
    Returns:
        HTML with pagination controls and initialization script
    """
    wrapper_id = f"{table_id}_wrapper"

    # Set default empty strings to avoid None in HTML attributes
    # 設置默認空字符串以避免 HTML 屬性中出現 None
    start_attr = start_date or ""
    end_attr   = end_date or ""

    # Initialize pagination via external JavaScript function from dashboard.js
    # This runs after DOM is fully loaded to ensure table exists
    # 通過 dashboard.js 中的外部 JavaScript 函數初始化分頁
    # 在 DOM 完全加載後運行以確保表格存在
    js = f"""
<script>
document.addEventListener("DOMContentLoaded", function() {{
  if (typeof window.initTablePagination === "function") {{
    window.initTablePagination("{table_id}", "{wrapper_id}", {page_size});
  }}
}});
</script>
"""

    # Pagination bar with first/prev/next/last buttons and page counter
    # Uses HTML entities for arrow symbols: &lt;&lt; = <<, &lt; = <, etc.
    # 分頁欄帶有首頁/上一頁/下一頁/末頁按鈕和頁面計數器
    # 使用 HTML 實體表示箭頭符號：&lt;&lt; = <<，&lt; = < 等
    controls_html = f"""
<div class="table-pagination-wrapper">
  <div class="table-pagination">
    <button class="page-btn first-btn" type="button">&lt;&lt;</button>
    <button class="page-btn prev-btn"  type="button">&lt;</button>

    <span class="page-info">
        <span class="current-page">1</span>
        <span class="page-slash">/</span>
        <span class="total-pages">1</span>
    </span>

    <button class="page-btn next-btn" type="button">&gt;</button>
    <button class="page-btn last-btn" type="button">&gt;&gt;</button>
  </div>
</div>
"""

    return f"""
<div id="{wrapper_id}" data-start-date="{start_attr}" data-end-date="{end_attr}">
  <div class="table-scroll-wrapper">
    {table_html}
  </div>
  {controls_html}
</div>
{js}
"""


def generate_table_section_html(
    table_html: str,
    title: str,
    table_id: str,
    section_id: str | None = None,
    export_visible: bool = True,
) -> str:
    """
    Generate a complete table section with title and export button.
    用於生成完整的表格區段，包含標題和匯出按鈕
    
    This wraps the table in a standard block with:
    - Title header
    - Export button (with CSV download icon)
    - The table content itself
    
    Args:
        table_html: The table HTML content (can be from create_styled_table or wrap_table_with_pagination)
        title: The section title (e.g., "最大5個回撤", "交易紀錄")
        table_id: The ID of the table element (for CSV export targeting)
        section_id: Optional ID for the outer div (defaults to f"{table_id}_section")
        export_visible: Whether to respect filter when exporting (True = export filtered rows only)
    
    Returns:
        Complete HTML section with title, export button, and table
    """
    if section_id is None:
        section_id = f"{table_id}_section"
    
    export_attr = "1" if export_visible else "0"
    
    return f"""
<div id="{section_id}" class="block--mb">
  <div class="blockTitle-row">
    <h3 class="blockTitle">{title}</h3>
    
    <!-- Export button: note data-table-id + data-export-visible -->
    <button
      type="button"
      class="export-btn"
      data-table-id="{table_id}"
      data-export-visible="{export_attr}"
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="7 10 12 15 17 10"></polyline>
        <line x1="12" y1="15" x2="12" y2="3"></line>
      </svg>
    </button>
  </div>
  {table_html}
</div>
"""


def generate_table_section_with_date_filters_html(
    table_html: str,
    title: str,
    table_id: str,
    section_id: str | None = None,
    export_visible: bool = True,
    start_input_id: str | None = None,
    end_input_id: str | None = None,
) -> str:
    """
    Generate table section with title, export button, and date range filters.
    用於生成包含日期範圍篩選的表格區段
    
    Args:
        table_html: The table HTML (usually from wrap_table_with_pagination)
        title: Section title (e.g., "每日摘要")
        table_id: Table element ID for CSV export
        section_id: Optional outer div ID
        export_visible: Whether export respects filters
        start_input_id: ID for start date input (defaults to f"{table_id}-start")
        end_input_id: ID for end date input (defaults to f"{table_id}-end")
    
    Returns:
        Complete HTML with title, export, date filters, and table
    """
    if section_id is None:
        section_id = f"{table_id}_section"
    if start_input_id is None:
        start_input_id = f"{table_id.replace('_table', '')}-start"
    if end_input_id is None:
        end_input_id = f"{table_id.replace('_table', '')}-end"
    
    export_attr = "1" if export_visible else "0"
    
    return f"""
<div id="{section_id}" class="block--mb">
  <div class="blockTitle-row">
    <h3 class="blockTitle">{title}</h3>
    
    <button
      type="button"
      class="export-btn"
      data-table-id="{table_id}"
      data-export-visible="{export_attr}"
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="7 10 12 15 17 10"></polyline>
        <line x1="12" y1="15" x2="12" y2="3"></line>
      </svg>
    </button>
  </div>
  
  <!-- Date range filters -->
  <div class="inputrow-area">
    <div class="inputrow">
      <label class="inputrow-title">期間</label>
      <div class="inputrow-inner inputrow--date">
        <div class="inputrow">
          <label class="inputrow-title w-auto">開始日期</label>
          <input type="date" id="{start_input_id}" class="inputbox" />
        </div>
        <div class="inputrow">
          <label class="inputrow-title w-auto">結束日期</label>
          <input type="date" id="{end_input_id}" class="inputbox" />
        </div>
      </div>
    </div>
  </div>
  
  {table_html}
</div>
"""


def generate_table_section_with_position_filters_html(
    table_html: str,
    title: str,
    table_id: str,
    position_options_html: str = "",
    section_id: str | None = None,
    export_visible: bool = True,
    symbol_select_id: str | None = None,
    start_input_id: str | None = None,
    end_input_id: str | None = None,
) -> str:
    """
    Generate table section with stock symbol + date range filters (for position table).
    用於生成包含股票代號和日期範圍篩選的表格區段（部位表格專用）
    
    Args:
        table_html: The table HTML (usually from wrap_table_with_pagination)
        title: Section title (e.g., "部位明細")
        table_id: Table element ID
        position_options_html: HTML options for stock symbol dropdown
        section_id: Optional outer div ID
        export_visible: Whether export respects filters
        symbol_select_id: ID for symbol dropdown (defaults to "position-symbol")
        start_input_id: ID for start date (defaults to "position-start")
        end_input_id: ID for end date (defaults to "position-end")
    
    Returns:
        Complete HTML with title, export, symbol+date filters, and table
    """
    if section_id is None:
        section_id = f"{table_id}_section"
    if symbol_select_id is None:
        symbol_select_id = "position-symbol"
    if start_input_id is None:
        start_input_id = "position-start"
    if end_input_id is None:
        end_input_id = "position-end"
    
    export_attr = "1" if export_visible else "0"
    
    return f"""
<div id="{section_id}" class="block--mb">
  <div class="blockTitle-row">
    <h3 class="blockTitle">{title}</h3>
    
    <button
      type="button"
      class="export-btn"
      data-table-id="{table_id}"
      data-export-visible="{export_attr}"
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="7 10 12 15 17 10"></polyline>
        <line x1="12" y1="15" x2="12" y2="3"></line>
      </svg>
    </button>
  </div>
  
  <div class="inputrow-area">
    <!-- Stock code filter -->
    <div class="inputrow">
      <label class="inputrow-title">股票代號</label>
      <div class="inputrow-inner">
        <select id="{symbol_select_id}" class="dash-dropdown inputbox stock-id-selector">
          <option value="all">全部部位</option>
          {position_options_html}
        </select>
      </div>
    </div>
    
    <!-- Date range filters -->
    <div class="inputrow">
      <label class="inputrow-title">期間</label>
      <div class="inputrow-inner inputrow--date">
        <div class="inputrow">
          <label class="inputrow-title w-auto">開始日期</label>
          <input type="date" id="{start_input_id}" class="inputbox" />
        </div>
        <div class="inputrow">
          <label class="inputrow-title w-auto">結束日期</label>
          <input type="date" id="{end_input_id}" class="inputbox" />
        </div>
      </div>
    </div>
  </div>
  
  {table_html}
</div>
"""
