from __future__ import annotations
"""Dashboard HTML Template Generator
儀表板 HTML 模板生成器

Generates interactive backtesting dashboard HTML with charts, tables, and metrics.
Placeholders are replaced with content from html_util.py generators.
生成包含圖表、表格和指標的互動式回測儀表板 HTML。
佔位符由 html_util.py 生成器的內容替換。
"""

from datetime import datetime
from pathlib import Path
from IPython.display import HTML, display
import json, html as _html, random as _random, string as _string
from plotly.io import to_html
from dash import dcc, html
import plotly.graph_objs as go
import pandas as pd
import plotly.io as pio
import webbrowser

# ============================================================================
# Asset Loading: Load external CSS and JavaScript files
# 資源載入：載入外部 CSS 和 JavaScript 檔案
# ============================================================================
css_path = Path(__file__).parent / "assets" / "dashboard.css"
css_content = css_path.read_text(encoding="utf-8")
plotly_js_path = Path(__file__).parent / "assets" / "plotly-2.29.1.min.js"
plotly_js_content = plotly_js_path.read_text(encoding="utf-8")
dashboard_js_path = Path(__file__).parent / "assets" / "dashboard.js"
dashboard_js_content = dashboard_js_path.read_text(encoding="utf-8")

# ============================================================================
# Version Tracking: Embedded in HTML output for debugging
# 版本追蹤：嵌入到 HTML 輸出中用於除錯
# ============================================================================
# Dashboard version for tracking template changes
# 儀表板版本用於追蹤模板變更
DASHBOARD_VERSION = "1.0.0"

# ============================================================================
# Placeholder Validation Lists
# 佔位符驗證列表
# ============================================================================
# Required placeholders that should be present in html_content
# 必需的佔位符，應該存在於 html_content 中
REQUIRED_PLACEHOLDERS = [
    "_BASE_METRICS_HTML_",
    "_RETURN_METRICS_HTML_",
    "_ADDITIONAL_METRICS_HTML_",
    "_REMAINDER_TABLE_HTML_",
    "_PIE_CHART_HTML_",
    "_HEATMAP_HTML_",
    "_DRAWDOWN_GRAPH_HTML_",
    "_UNDERWATER_GRAPH_HTML_",
    "_PNL_GRAPH_HTML_",
    "_PNL_COST_GRAPH_HTML_",
    "_MAX5_DRAWDOWN_SECTION_",
    "_TRADE_TABLE_SECTION_",
    "_DAILY_TABLE_SECTION_",
    "_POSITION_TABLE_SECTION_",
]

# Optional placeholders (won't trigger warnings if missing)
# 可選的佔位符（缺失時不會觸發警告）
OPTIONAL_PLACEHOLDERS = [
]

# ============================================================================
# Main HTML Template: Complete dashboard structure with placeholders
# 主 HTML 模板：包含佔位符的完整儀表板結構
# ============================================================================
HTML_CONTENT = f"""
<!DOCTYPE html>
<html lang="zh-Hant" data-dashboard-version="{DASHBOARD_VERSION}">
<head>
    <meta charset="UTF-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>回測報告</title>

    <!-- Plotly bundle once, globally -->
    <script>
    {plotly_js_content}
    </script>
    <!-- Injected dashboard.js -->
    <script>
    {dashboard_js_content}
    </script>
    <!-- Injected CSS from assets/dashboard.css -->
    <style>
    {css_content}
    </style>
</head>

<body>
<div>
    <div id="react-entry-point">
      <div>
        <!-- Heading -->
        <h1>回測報告</h1>
        <div style="min-height: 100vh; padding: 1rem;
    max-width: 1600px;
    margin: 0px auto;
    box-sizing: border-box;">
        <!-- FIRST ROW: TOP METRICS -->
        <div id="metrics-grid" class="dashFeature">
            <div class="featureBox" id="top-metrics">
              _BASE_METRICS_HTML_
            </div>
        </div>

        <!-- SECOND ROW -->
        <div id="second-row">
            <!-- Left side: indicators + additional metrics -->
            <div id="left-side-container">
                <div class="featureBox featureBox--main" id="indicators-container">
                    _RETURN_METRICS_HTML_
                </div>
                <div id="additional-metrics-container" class="featureBox--sub">
                    _ADDITIONAL_METRICS_HTML_
                </div>
            </div>

            <!-- Middle: pie chart -->
            <div id="pie-chart-container">
                _PIE_CHART_HTML_
                
            </div>

            <!-- Right: table -->
            <div class="table-section">
                _REMAINDER_TABLE_HTML_
            </div>
        </div>

        <!-- THIRD ROW -->
        <div id="heatmap-row">
            
                _HEATMAP_HTML_
           
        </div>

        <!-- Fourth Row -->
        <div class="two-figure-container">
            <div class="two-figure-item">
                    _DRAWDOWN_GRAPH_HTML_
            </div>
            <div class="two-figure-item">
                _UNDERWATER_GRAPH_HTML_
            </div>
           
        </div>
        <!-- Fifth Row -->
        <div class="two-figure-container">
            <div class="two-figure-item">

                _PNL_GRAPH_HTML_
            </div>
            <div class="two-figure-item">
                _PNL_COST_GRAPH_HTML_
            </div>
        </div>

        
        <!-- SIXTH ROW: Top 5 Drawdowns -->
        _MAX5_DRAWDOWN_SECTION_

        <!-- 7th Row: Trade Table -->
        _TRADE_TABLE_SECTION_

        <!-- 8th Row: Daily Table -->
        _DAILY_TABLE_SECTION_
        
        <!-- 9th Row: Position Table -->
        _POSITION_TABLE_SECTION_


    </div>


      </div>
    </div>
</div>

</body>
</html>
"""





import html as _html
import random as _random
import string as _string

# ============================================================================
# Iframe Wrapper Functions: For notebook display with auto-resize capability
# Iframe 包裝函數：用於筆記本顯示，具有自動調整大小功能
# ============================================================================

def make_iframe_html(
    srcdoc_html: str,
    channel_id: str | None = None,
    height: int = 900,
    width: str = "100%",
    max_width: str | None = None,
    scrollable: bool = False,
) -> str:
    """
    Wrap the given HTML in an <iframe> for notebook display.
    將給定的 HTML 包裝在 <iframe> 中以在筆記本中顯示。

    scrollable = False  -> auto-resize iframe to fit content (no scrollbars)
                          自動調整 iframe 大小以適應內容（無滾動條）
    scrollable = True   -> fixed iframe height, vertical scrolling if content taller
                          固定 iframe 高度，如果內容較高則垂直滾動
    """
    import random as _random
    import string as _string
    import html as _html

    # Generate unique IDs for communication channel and iframe element
    # 為通信頻道和 iframe 元素生成唯一 ID
    if channel_id is None:
        channel_id = "ch_" + "".join(_random.choice(_string.ascii_letters) for _ in range(8))
    iframe_id = "iframe_" + "".join(_random.choice(_string.ascii_letters) for _ in range(8))

    # Escape HTML for safe embedding in srcdoc attribute
    # 轉義 HTML 以安全嵌入 srcdoc 屬性
    escaped = _html.escape(srcdoc_html, quote=True)

    max_width_style = f"max-width:{max_width};" if max_width else ""
    scrolling_attr = "yes" if scrollable else "no"

    # Parent-side resize script: listens to postMessage from child iframe
    # 父級調整大小腳本：監聽來自子 iframe 的 postMessage
    # Only needed for auto-resize mode (scrollable=False)
    # 僅在自動調整大小模式下需要（scrollable=False）
    resize_script = ""
    if not scrollable:
        resize_script = f"""
<script>
(function() {{
    const iframe = document.getElementById("{iframe_id}");
    window.addEventListener("message", function(e) {{
        if (e.data && e.data.channel === "{channel_id}" && e.data.height) {{
            iframe.style.height = e.data.height + "px";
        }}
    }});
}})();
</script>
"""

    

    return f"""
<div id="iframe-wrapper" style="position:relative; width:100%;">
  <iframe id="{iframe_id}"
          srcdoc="{escaped}"
          style="height:{int(height)}px; width:{width}; {max_width_style}
                 border:none; border-radius:12px; display:block;"
          scrolling="{scrolling_attr}">
  </iframe>
</div>
{resize_script}
"""
def wrap_child_with_resizer(html: str, channel_id: str) -> str:
    """
    Inject resize detection script into child HTML for auto-resize iframe.
    將調整大小檢測腳本注入子 HTML 以實現自動調整 iframe。
    
    Sends height updates to parent via postMessage when content size changes.
    當內容大小變化時，通過 postMessage 向父級發送高度更新。
    """
    # Script that measures document height and sends to parent
    # 測量文檔高度並發送給父級的腳本
    injector = f"""
  <script>
    (function(){{
      const channel = {channel_id!r};

      function sendSize(reason) {{
        const h = Math.max(
          document.documentElement.scrollHeight || 0,
          document.body.scrollHeight || 0
        );
        parent.postMessage({{ type: 'IFRAME_RESIZE', channel, height: h, reason }}, '*');
      }}

      // Measure once on load + once after a short delay
      window.addEventListener('load', () => sendSize('load'));
      window.addEventListener('pageshow', () => sendSize('pageshow'));
      setTimeout(() => sendSize('timeout'), 300);
    }})();
  </script>
</body>
</html>"""

    # Inject script before closing tags to ensure it runs after content loads
    # 在關閉標籤之前注入腳本以確保它在內容載入後運行
    if "</body>" in html:
        head, tail = html.rsplit("</body>", 1)
        if "</html>" in tail:
            tail_head, tail_tail = tail.split("</html>", 1)
            return head + injector + tail_tail
        else:
            return head + injector
    elif "</html>" in html:
        head, tail = html.rsplit("</html>", 1)
        return head + injector
    else:
        return html + injector

from pathlib import Path
from IPython.display import HTML, display

# ============================================================================
# Main Dashboard Function: Generate and display/save the dashboard
# 主儀表板函數：生成並顯示/保存儀表板
# ============================================================================

def in_notebook():
    try:
        from IPython import get_ipython
        shell = get_ipython().__class__.__name__
        return shell == "ZMQInteractiveShell"
    except:
        return False
    

def dashboard(
    html_content:dict,
    path: str | None = None,
    scrollable: bool = True,    
    frame_height: int = 900,
    max_width: str | None = None,
    view: str | None = None
):
    """
    Generate dashboard HTML by replacing placeholders with content.
    通過用內容替換佔位符來生成儀表板 HTML。
    
    Args:
        html_content: Dict mapping placeholder names to HTML content
                     將佔位符名稱映射到 HTML 內容的字典
        path: File path to save HTML. If None, uses auto-generated name with date
             保存 HTML 的檔案路徑。如果為 None，使用帶日期的自動生成名稱
        scrollable: If True, fixed height with scrollbars; if False, auto-resize
                   如果為 True，固定高度帶滾動條；如果為 False，自動調整大小
        frame_height: Initial iframe height in pixels
                     初始 iframe 高度（像素）
        max_width: Optional max width for iframe
                  iframe 的可選最大寬度
        view: Display mode - 'web': save and open in browser
                            'notebook': display in Jupyter
                            None: save only, no display/open
             顯示模式 - 'web'：保存並在瀏覽器中打開
                       'notebook'：在 Jupyter 中顯示
                       None：僅保存，不顯示/打開
    """
    # Step 1: Validate required placeholders are present
    # 步驟 1：驗證所需的佔位符是否存在
    missing = [p for p in REQUIRED_PLACEHOLDERS if p not in html_content]
    if missing:
        print(f"⚠️  Warning: Missing content for placeholders: {', '.join(missing)}")
    
    # Step 2: Replace all placeholders with provided content
    # 步驟 2：用提供的內容替換所有佔位符
    final_html = HTML_CONTENT
    for placeholder, content in html_content.items():
        final_html = final_html.replace(placeholder, content or "")
    
    # Step 3: Remove optional placeholders if not provided (avoid empty sections)
    # 步驟 3：如果未提供則移除可選佔位符（避免空白區域）
    for placeholder in OPTIONAL_PLACEHOLDERS:
        if placeholder not in html_content or not html_content.get(placeholder):
            final_html = final_html.replace(placeholder, "")
  
    # Step 4: Generate unique channel ID for iframe communication
    # 步驟 4：為 iframe 通信生成唯一頻道 ID
    channel_id = "ch_" + "".join(_random.choice(_string.ascii_letters) for _ in range(8))

    # Step 5: Choose display mode based on scrollable parameter
    # 步驟 5：根據 scrollable 參數選擇顯示模式
    if scrollable:
        # Mode A: Fixed height with scrollbars inside iframe
        # 模式 A：固定高度，iframe 內有滾動條
        parent_html = make_iframe_html(
            final_html,
            channel_id=channel_id,
            height=frame_height,
            width="100%",
            max_width=max_width,
            scrollable=True,
        )
    else:
        # Mode B: Auto-resize iframe to fit content (no scrollbars)
        # 模式 B：自動調整 iframe 以適應內容（無滾動條）
        child_wrapped = wrap_child_with_resizer(final_html, channel_id)
        parent_html = make_iframe_html(
            child_wrapped,
            channel_id=channel_id,
            height=frame_height,   # initial height, will be overridden
            width="100%",
            max_width=max_width,
            scrollable=False,
        )

    # Step 6: Save to file if path is provided or view requires it
    # 步驟 6：如果提供了路徑或視圖需要則保存到檔案
    save_file =True
    if path is None and view == "notebook" and in_notebook():
        save_file = False

    if save_file:    
        if path is None:
            # Auto-generate filename with date and incremental number
            # 自動生成帶日期和遞增編號的檔名
            today_str = datetime.today().strftime("%Y%m%d")
            base_name = f"{today_str}_dashboard"
            Path.cwd().mkdir(parents=True, exist_ok=True)

            # Find next available file number to avoid overwriting
            # 查找下一個可用的檔案編號以避免覆蓋
            index = 1
            while True:
                file_name = f"{base_name}_{index}.html"
                full_path = Path.cwd() / file_name
                if not full_path.exists():
                    break
                index += 1
            output_path = full_path
        else:
            output_path = Path(path)
        
        # Save HTML file
        # 保存 HTML 檔案
        output_path.write_text(final_html, encoding="utf-8")
        print(f"Dashboard saved to {output_path.resolve()}")
    
    # Step 7: Handle display/open based on view mode
    # 步驟 7：根據視圖模式處理顯示/打開
    if view == "web":
        # Web mode: Open saved file in browser
        # 網頁模式：在瀏覽器中打開保存的檔案
        webbrowser.open(output_path.resolve().as_uri(), new=2)
    elif view == "notebook":
        # Notebook mode: Display in Jupyter with iframe wrapper (no file saved if path=None)
        # 筆記本模式：在 Jupyter 中使用 iframe 包裝顯示（如果 path=None 則不保存檔案）
        display(HTML(parent_html))
    # If view is None: file is saved but not opened/displayed
    # 如果 view 為 None：檔案已保存但不打開/顯示

