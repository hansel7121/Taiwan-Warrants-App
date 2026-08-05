import plotly.graph_objects as go
import pandas as pd
from dash import dash_table
from plotly.subplots import make_subplots
import inspect
import numpy as np
import dash
from dash import dcc, html
from dash.dependencies import Input, Output
from dash import State, dash_table


def translate_df_cols(df, table_id):
    """
    Translate column names of a DataFrame to traditional chinese based on the specified table ID.

    Parameters:
    -----------
    df : pandas.DataFrame
        The input DataFrame to be translated
    table_id : str
        Identifier for the type of table ('trade_table', 'daily_table', or 'position_table')

    Returns:
    --------
    pandas.DataFrame
        DataFrame with translated column names
    """
    translation_maps = {
        "trade_table": {
            "id": "股票代號",
            "direction": "多空策略",
            "type": "狀況",
            "entry_time": "入場時間",
            "entry_price": "入場價格",
            "exit_time": "出場時間",
            "exit_price": "出場價格",
            "current_time": "當前時間",
            "current_price": "當前價格",
            "qty": "數量",
            "tax": "稅",
            "fee": "手續費",
            "cost": "成本",
            "entry_amount": "入場金額",
            "exit_amount": "出場金額",
            "predict": "預測",
            "pnl": "損益",
            "returns": "報酬率",
        },
        "daily_table": {
            "date": "日期",
            "daytrade": "當沖價金",
            "cashflow": "現金流",
            "entry_cost": "持倉成本",
            "market_value": "持倉市值",
            "profit_loss": "累計損益",
            "unrealized":"未實現損益",
            'realized':'已實現損益',
            'invest':'投入資金',
            'PNL/COST':'pnl/累計交易金額',
            "return": "日報酬",
            "return_pct": "日變動比率",
            "benchmark":"對標價格",
            "benchmark_return":"對標價格日報酬",
            "benchmark_return_pct":"對標價格日變動比率",
        },
        "position_table": {
            "date": "日期",
            "id": "股票代號",
            "direction": "多空策略",
            "qty": "數量",
            "entry_date": "入場日期",
            "entry_price": "入場價格",
            "CLOSE": "收盤價",
            "entry_cost": "入場成本",
            "market_value": "市值",
            "pnl": "損益",
        },
         "longest_drawdown_table":{
        'start': '開始日期',
        'valley': '最低點日期',
        'end': '結束日期',
        'days': '持續天數',
        'max drawdown': '最大回撤百分比'
    }
    }

    # Check if the table_id is valid
    if table_id in translation_maps:

        # Rename columns
        df.rename(columns=translation_maps[table_id], inplace=True)



def _process_df(df, table_id=None):
    """
        df (pd.DataFrame): Input DataFrame to display
        table_id (str): Optional ID for the table component for callbacks
    """
    df = df.copy()

    # Format datetime columns to show only the date
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime("%Y-%m-%d")
        elif isinstance(df[col].dtype, pd.DatetimeTZDtype):
            df[col] = df[col].dt.strftime("%Y-%m-%d")
        elif isinstance(df[col], str) and "T00:00:00" in df[col]:
            # Handle string datetime format
            df[col] = pd.to_datetime(df[col]).strftime("%Y-%m-%d")

    translate_df_cols(df, table_id)
    return df

# preprocessing for the upper half of the dashboard content before the heatmap
def process_for_dashboard(df):

    df = df.copy()
    # Replace None and NaN values with pd.NA for consistency
    df = df.replace([None, "None", "Nan", "NaN", float("nan")], pd.NA)

    # Assuming your dataframe is named df
    metrics = [
        {
            "value": (
                f"{(float(df.loc['TotalReturns', 'Strategy']) * 100):.2f}%"
                if pd.notna(df.loc["TotalReturns", "Strategy"])
                else "N/A"
            ),
            "label": "總報酬率",
        },
        {
            "value": (
                f"{(float(df.loc['CAGR', 'Strategy']) * 100):.2f}%"
                if pd.notna(df.loc["CAGR", "Strategy"])
                else "N/A"
            ),
            "label": "年化報酬率",
        },
        {
            "value": (
                f"{float(df.loc['SharpeRatio', 'Strategy']):.2f}"
                if pd.notna(df.loc["SharpeRatio", "Strategy"]) 
                else "N/A"
            ),
            "label": "Sharpe Ratio",
        },
        {
            "value": (
                f"{float(df.loc['WinRate', 'Strategy']):.2f}"
                if pd.notna(pd.to_numeric(df.loc["WinRate", "Strategy"], errors='coerce'))
                else "N/A"
            ),
            "label": "勝率",
        },
        {
            "value": (
                f"{(float(df.loc['MaxDrawdown', 'Strategy']) * 100):.2f}%"
                if pd.notna(df.loc["MaxDrawdown", "Strategy"])
                else "N/A"
            ),
            "label": "最大回撤",
        },
        {
            "value": (
                f"{int(df.loc['MddDuration', 'Strategy'])}"
                if pd.notna(df.loc["MddDuration", "Strategy"])
                else "N/A"
            ),
            "label": "最大回撤天數",
        },
        {
            "value": (
                f"{float(df.loc['SortinoRatio', 'Strategy']):.2f}"
                if pd.notna(df.loc["SortinoRatio", "Strategy"])
                else "N/A"
            ),
            "label": "Sortino Ratio",
        },
        {
            "value": (
                f"{float(df.loc['ProfitLossRatio', 'Strategy']):.2f}"
                if pd.notna(pd.to_numeric(df.loc["ProfitLossRatio", "Strategy"], errors='coerce'))
                else "N/A"
            ),
            "label": "盈虧比",
        },
    ]

    return_rates = [
        {
            "label": "周報酬率",
            "value": (
                float(df.loc["WeeklyReturn", "Strategy"].strip("%"))
                if pd.notna(df.loc["WeeklyReturn", "Strategy"])
                else 0
            ),
        },
        {
            "label": "月報酬率",
            "value": (
                float(df.loc["MonthlyReturn", "Strategy"].strip("%"))
                if pd.notna(df.loc["MonthlyReturn", "Strategy"])
                else 0
            ),
        },
        {
            "label": "季報酬率",
            "value": (
                float(df.loc["QuarterlyReturn", "Strategy"].strip("%"))
                if pd.notna(df.loc["QuarterlyReturn", "Strategy"])
                else 0
            ),
        },
        {
            "label": "年報酬率",
            "value": (
                float(df.loc["AnnualReturn", "Strategy"].strip("%"))
                if pd.notna(df.loc["AnnualReturn", "Strategy"])
                else 0
            ),
        }
    ]

    additional_metrics = [
        {
            "label": "交易次數",
            "value": (
                f"{int(df.loc['TotalTrades', 'Strategy'])}"
                if pd.notna(df.loc["TotalTrades", "Strategy"])
                else "N/A"
            ),
        },
        {
            "label": "獲利交易次數",
            "value": (
                f"{int(df.loc['ProfitTimes', 'Strategy'])}"
                if pd.notna(df.loc["ProfitTimes", "Strategy"])
                else "N/A"
            ),
        },
        {
            "label": "虧損交易次數",
            "value": (
                f"{int(df.loc['LossTimes', 'Strategy'])}"
                if pd.notna(df.loc["LossTimes", "Strategy"])
                else "N/A"
            ),
        },
        # {
        #     "label": "交易費用",
        #     "value": (
        #         f"{int(df.loc['TradeCost', 'Strategy'])}"
        #         if pd.notna(df.loc["TradeCost", "Strategy"])
        #         else "N/A"
        #     ),
        # },
        # {
        #     "label": "平均交易盈虧",
        #     "value": (
        #         f"{float(df.loc['AvgProfitPerTrade', 'Strategy']):.2f}"
        #         if pd.notna(df.loc["AvgProfitPerTrade", "Strategy"])
        #         else "N/A"
        #     ),
        # },
    ]

    # Create a new dataframe using the specified rows and their values
    new_df_data = {
        "資料明細": [
            "第一筆交易的日期",
            "最新資料日期",
            "回測執行日期",
            "最大倉位所需資金",
            "淨利潤",
            "交易費用",
            "平均交易盈虧"
        ],
        "數值": [
            df.loc["StartDate", "Strategy"],
            df.loc["EndDate", "Strategy"],
            df.loc["LastSignalDate", "Strategy"],
            df.loc["Cash", "Strategy"],
            df.loc["NetProfit", "Strategy"],
            f"{int(df.loc['TradeCost', 'Strategy'])}",
            f"{float(df.loc['AvgProfitPerTrade', 'Strategy']):.2f}",
        ],
    }

    # Convert the dictionary to a dataframe
    new_df = pd.DataFrame(new_df_data)

    return metrics, return_rates, additional_metrics, new_df



# ------------------------separate figs----------
def heatmap_returns(
    returns,
    annot_size=10,
    returns_label="回測",  # 回測名字，建議從backtest帶入
    grayscale=False,
    show=True,
    compounded=False,
    saveFig=False,
):
    """
    Create a monthly and yearly returns heatmap using Plotly
    """
    import plotly.graph_objects as go
    import pandas as pd
    import numpy as np
    from plotly.subplots import make_subplots

    # Create monthly returns heatmap
    offset = "ME" if pd.__version__ >= "2.2.0" else "M"
    df = returns.resample(offset).sum().to_frame(name="Returns")
    df.columns = ["Returns"]
    df["Month"] = df.index.strftime("%b").str.upper()
    df["Year"] = df.index.year
    df = df.pivot(index="Year", columns="Month", values="Returns").fillna(0)
    month_order = [
        "JAN",
        "FEB",
        "MAR",
        "APR",
        "MAY",
        "JUN",
        "JUL",
        "AUG",
        "SEP",
        "OCT",
        "NOV",
        "DEC",
    ]
    #returns_monthly = df[month_order] * 100
    returns_monthly = df.reindex(columns=month_order, fill_value=0) * 100
    z_data_monthly = returns_monthly

    # Create yearly returns DataFrame
    returns_yearly = pd.DataFrame(
        group_returns(returns, returns.index.strftime("%Y"), compounded)
    )
    returns_yearly.columns = ["Returns"]
    returns_yearly.index = pd.to_datetime(returns_yearly.index)
    returns_yearly["Year"] = returns_yearly.index.strftime("%Y")
    returns_yearly["Returns"] = returns_yearly["Returns"] * 100
    returns_matrix = returns_yearly.set_index("Year").T
    returns_matrix = returns_matrix.rename_axis("columns").T

    # previous color scheme
    # colorscale = [
    #         [0.0, 'rgb(165,0,38)'],
    #         [0.1111111111111111, 'rgb(215,48,39)'],
    #         [0.2222222222222222, 'rgb(244,109,67)'],
    #         [0.3333333333333333, 'rgb(253,174,97)'],
    #         [0.4444444444444444, 'rgb(254,224,144)'],
    #         [0.5555555555555556, 'rgb(224,243,248)'],
    #         [0.6666666666666666, 'rgb(171,217,233)'],
    #         [0.7777777777777778, 'rgb(116,173,209)'],
    #         [0.8888888888888888, 'rgb(69,117,180)'],
    #         [1.0, 'rgb(49,54,149)']
    #     ]
    # Set color scheme
    colorscale = (
        "gray"
        if grayscale
        else [
            [0.0, "rgb(0,64,0)"],
            [0.25, "rgb(116,173,209)"],
            [0.5, "rgb(230, 230, 230)"],
            [0.75, "rgb(244,109,67)"],
            [1.0, "rgb(165,0,38)"],
        ]
    )

    # Create a figure with custom widths for the subplots
    fig = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=("年回報率(%)", f"{returns_label} -月回報率(%)"),
        horizontal_spacing=0.1,
        column_widths=[0.05, 0.9],  # Make yearly heatmap narrower on the left
    )

    # Create the yearly heatmap trace
    yearly_trace = go.Heatmap(
        z=returns_matrix.values,
        x=["Year"],  # Single column for yearly returns
        y=returns_matrix.index,
        colorscale=colorscale,
        zmid=0,
        text=returns_matrix.round(2).values,
        texttemplate="%{text:.2f}%",
        textfont={"size": annot_size},
        hoverongaps=False,
        hovertemplate="Year: %{y}<br>Returns: %{text}%<extra></extra>",
        coloraxis="coloraxis1",
    )

    # Create the monthly heatmap trace
    monthly_trace = go.Heatmap(
        z=z_data_monthly.values,
        x=month_order,
        y=z_data_monthly.index.astype(str),
        colorscale=colorscale,
        zmid=0,
        text=z_data_monthly.round(2).values,
        texttemplate="%{text:.2f}%",
        textfont={"size": annot_size},
        hoverongaps=False,
        hovertemplate="Year: %{y}<br>Month: %{x}<br>Returns: %{text}%<extra></extra>",
        coloraxis="coloraxis2",  # Separate color axis for monthly heatmap
    )

    # Add traces to subplots
    fig.add_trace(yearly_trace, row=1, col=1)
    fig.add_trace(monthly_trace, row=1, col=2)

    # Update layout with separate color axes
    fig.update_layout(
        coloraxis1=dict(
            colorscale=colorscale,
            # colorbar_title="Yearly Returns (%)",
            colorbar_thickness=10,
            colorbar_len=0.8,
            colorbar_x=-0.1,  # Position colorbar for yearly returns
            colorbar_y=0.5,
            colorbar_yanchor="middle",
        ),
        coloraxis2=dict(
            colorscale=colorscale,
            # colorbar_title="Monthly Returns (%)",
            colorbar_thickness=20,
            colorbar_len=0.8,
            colorbar_x=1.02,  # Position colorbar for monthly returns
            colorbar_y=0.5,
            colorbar_yanchor="middle",
        ),
        autosize=True,
        title={
            "text": f"{returns_label} - 回報率熱量圖",
            "y": 0.02,
            "x": 0.5,
            "xanchor": "center",
            "yanchor": "bottom",
            #"font": {"size": 16, "weight": "bold"},
            "font": {"size": 16},


        },
        paper_bgcolor="white",
        plot_bgcolor="white",
        showlegend=False,
        margin=dict(t=50, b=80, l=20, r=80),
    )

    # Update axes for both heatmaps
    fig.update_xaxes(side="bottom", tickangle=0)
    fig.update_yaxes(autorange="reversed", side="left")

    # if saveFig:
    #     fig.write_image(**saveFig, width=1400, height=500)
    #     return None

    if saveFig:
        if isinstance(saveFig, dict):
            fig.write_image(**saveFig)
        else:
            fig.write_image(saveFig, width=1400, height=600)

    if show:
        fig.show()
        return None
    return fig

def pnlcost_plt_plotly(
    returns,
    line_width=1.5,
    font_family="Arial",
    grayscale=False,
    title="平均報酬百分比圖",
    saveFig=None,
    show=True,
):
    """
    Plot the average operating profit rate using Plotly.

    Parameters:
    -----------
    returns : pd.Series or array-like
        Time series of PNL/COST values with datetime index
    line_width : float, default 1.5
        Width of the plot line
    font_family : str, default "Arial"
        Font family for text elements
    grayscale : bool, default False
        If True, uses grayscale instead of colors
    title : str, default "Net Profit Loss"
        Title of the plot
    savefig : str or None, default None
        If provided, saves the figure to the specified path
    show : bool, default True
        If True, displays the plot

    Returns:
    --------
    go.Figure or None
        Returns the figure object if show=False, otherwise None
    """

    if not isinstance(returns, pd.Series):
        if hasattr(returns, "index"):
            returns = pd.Series(returns.values, index=returns.index)
        else:
            returns = pd.Series(returns)

    line_color = "#000000" if grayscale else "#348dc1"
    grid_color = "#808080" if grayscale else "#E5E5E5"

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=returns.index,
            y=returns.values,
            mode="lines",
            #name="回測",
            showlegend=False,
            line=dict(color=line_color, width=line_width),
        )
    )

    fig.update_layout(
        title=dict(
            text=title,
            y=0.95,
            x=0.5,
            xanchor="center",
            yanchor="top",
            font=dict(family=font_family, size=20, color="black"),
        ),
        showlegend=True,
        plot_bgcolor="light blue",
        paper_bgcolor="white",
        xaxis=dict(
            title="日期",
            showgrid=True,
            gridcolor=grid_color,
            gridwidth=1,
            zeroline=False,
        ),
        yaxis=dict(
            title=dict(
                text="平均報酬率",
                font=dict(family=font_family, size=14, color="black"),
            ),
            showgrid=True,
            gridcolor=grid_color,
            gridwidth=1,
            zeroline=True,
            zerolinecolor=grid_color,
            zerolinewidth=1,
            tickformat=".1%",
            hoverformat=".1%",
        ),
        margin=dict(l=80, r=40, t=80, b=40),
    )

    if saveFig:
        if isinstance(saveFig, dict):
            fig.write_image(**saveFig)
        else:
            fig.write_image(saveFig, width=1200, height=500)

    if show:
        fig.show()
        return None
    return fig


def net_pnl_plotly(
    net_pnl,
    line_width=1.5,
    font_family="Arial",
    grayscale=False,
    title="淨利潤與損失",
    saveFig=None,
    show=True,
):
    """
    Create an interactive net profit/loss visualization using Plotly.

    Parameters:
    -----------
    net_pnl : pd.Series or array-like
        Time series of PNL values with datetime index
    line_width : float, default 1.5
        Width of the plot line
    font_family : str, default "Arial"
        Font family for text elements
    grayscale : bool, default False
        If True, uses grayscale instead of colors
    title : str, default "Net Profit Loss"
        Title of the plot
    savefig : str or None, default None
        If provided, saves the figure to the specified path
    show : bool, default True
        If True, displays the plot

    Returns:
    --------
    go.Figure or None
        Returns the figure object if show=False, otherwise None
    """

    if not isinstance(net_pnl, pd.Series):
        if hasattr(net_pnl, "index"):
            net_pnl = pd.Series(net_pnl.values, index=net_pnl.index)
        else:
            net_pnl = pd.Series(net_pnl)

    line_color = "#000000" if grayscale else "#348dc1"
    grid_color = "#808080" if grayscale else "#E5E5E5"

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=net_pnl.index,
            y=net_pnl.values,
            mode="lines",
            #name="回測",
            showlegend=False,
            line=dict(color=line_color, width=line_width),
        )
    )

    fig.update_layout(
        title=dict(
            text=title,
            y=0.95,
            x=0.5,
            xanchor="center",
            yanchor="top",
            font=dict(family=font_family, size=20, color="black"),
        ),
        showlegend=True,
        plot_bgcolor="light blue",
        paper_bgcolor="white",
        xaxis=dict(
            title="日期",
            showgrid=True,
            gridcolor=grid_color,
            gridwidth=1,
            zeroline=False,
        ),
        yaxis=dict(
            title=dict(
                text="淨利潤與損失(PNL)",
                font=dict(family=font_family, size=14, color="black"),
            ),
            showgrid=True,
            gridcolor=grid_color,
            gridwidth=1,
            zeroline=True,
            zerolinecolor=grid_color,
            zerolinewidth=1,
            tickformat=".2s",  # Format numbers as 1M for 1,000,000
            hoverformat=".2s",  # Show exact values in hover
        ),
        margin=dict(l=80, r=40, t=80, b=40),
    )

    if saveFig:
        if isinstance(saveFig, dict):
            fig.write_image(**saveFig)
        else:
            fig.write_image(saveFig, width=1200, height=500)

    if show:
        fig.show()
        return None
    return fig


def plot_longest_drawdowns_plotly(
    returns,
    periods=5,
    line_width=1.5,
    font_family="Arial",
    grayscale=False,
    title=None,  ##回測名字，建議從backtest帶入
    log_scale=False,
    show_ylabel=True,
    show_subtitle=True,
    compounded=True,
    benchmark=None,
    saveFig=None,
    show=True,
):
    """
    Plot the longest drawdown periods using Plotly.

    Parameters are similar to matplotlib version with slight adaptations for Plotly.
    """
    # Color scheme for the plot
    colors = ["#348dc1", "#003366", "red"] if not grayscale else ["#000000"] * 3

    # Calculate drawdowns from returns
    dd = to_drawdown_series(returns.fillna(0))
    # Get details of the longest drawdown periods
    dddf = drawdown_details(dd)
    longest_dd = dddf.sort_values(by="days", ascending=False).head(periods)

    # Calculate cumulative returns for the main series
    series = compsum(returns) if compounded else returns.cumsum()

    # Create the figure
    fig = go.Figure()

    # Add the main returns line to the plot
    fig.add_trace(
        go.Scatter(
            x=series.index,
            y=series,
            name="回測",
            line=dict(color=colors[0], width=line_width),
        )
    )

    # Add benchmark line if provided
    if benchmark is not None:
        benchmark_series = compsum(benchmark) if compounded else benchmark.cumsum()
        fig.add_trace(
            go.Scatter(
                x=benchmark_series.index,
                y=benchmark_series,
                name="Benchmark",
                line=dict(color="#FEDD78", width=line_width),
            )
        )

    # Highlight the drawdown periods as rectangles in the plot
    highlight_color = "black" if grayscale else "red"
    for _, row in longest_dd.iterrows():
        fig.add_vrect(
            x0=row["start"],
            x1=row["end"],
            fillcolor=highlight_color,
            opacity=0.1,
            layer="below",
            line_width=0,
        )

    # Update plot layout with title, labels, and other styling
    title_text = (
        f"{title} - 最大{periods:.0f}次回撤時期"
        if title
        else f"最大{periods:.0f}次回撤時期"
    )
    if show_subtitle:
        subtitle = f"{returns.index[0].strftime('%Y/%m/%d')} - {returns.index[-1].strftime('%Y/%m/%d')}"
        title_text = f"{title_text}<br><sup>{subtitle}</sup>"
    fig.update_layout(
        title=dict(
            text=title_text,
            font=dict(family=font_family, size=24, color="black"),
            x=0.5,
            xanchor="center",
        ),
        showlegend=True,
        legend=dict(x=0.01, y=0.99, xanchor="left", yanchor="top"),
        plot_bgcolor="white",
        paper_bgcolor="white",
        yaxis=dict(
            title="累積回報率" if show_ylabel else None,
            tickformat=".1%",
            showgrid=True,
            gridcolor="lightgray",
            type="log" if log_scale else "linear",
            zeroline=True,
            zerolinecolor="black",
            zerolinewidth=1,
        ),
        xaxis=dict(
            showgrid=True, gridcolor="lightgray", rangeslider=dict(visible=False)
        ),
        hovermode="x unified",
    )

    # Add a horizontal line at y=0 to indicate the zero returns level
    fig.add_hline(y=0, line=dict(color="black", width=1, dash="dash"))
    if saveFig:
        if isinstance(saveFig, dict):
            fig.write_image(**saveFig)
        else:
            fig.write_image(saveFig, width=1200, height=500)

    if show:
        fig.show()
        return None
    return fig


def drawdowns_periods_plotly(
    returns,
    periods=5,
    lw=1.5,
    log_scale=False,
    fontname="Arial",
    grayscale=False,
    title=None,
    ylabel=True,
    subtitle=True,
    compounded=True,
    saveFig=None,
    show=True,
    prepare_returns=True,
    benchmark=None,
):
    if prepare_returns:
        returns = _prepare_returns(returns)
        if isinstance(benchmark, pd.Series):
            benchmark = _prepare_returns(benchmark)

    fig = plot_longest_drawdowns_plotly(
        returns,
        periods=5,
        line_width=1.5,
        font_family="Arial",
        grayscale=False,
        title=title,
        log_scale=False,
        show_ylabel=True,
        show_subtitle=True,
        compounded=compounded,
        benchmark=benchmark,
        saveFig=saveFig,
        show=show,
    )
    if not show:
        return fig


def plot_timeseries_plotly(
    returns,
    benchmark=None,
    title="Returns",
    compound=False,
    cumulative=True,
    fill=True,
    returns_label="Strategy",
    hline=None,
    hlw=None,
    hlcolor="red",
    hllabel="",
    percent=True,
    match_volatility=False,
    log_scale=False,
    resample=None,
    line_width=1.5,
    ylabel="",
    grayscale=False,
    font_family="Arial",
    subtitle=True,
    saveFig=None,
    show=True,
):
    """
    Plot timeseries using Plotly
    """
    import pandas as pd
    import plotly.graph_objects as go

    # Color handling
    colors = (
        ["#000000", "#666666", "#999999"]
        if grayscale
        else ["#2196f3", "#4CAF50", "#FF9800"]
    )

    # Data preparation
    returns = returns.fillna(0)
    if isinstance(benchmark, pd.Series):
        benchmark = benchmark.fillna(0)

    # Match volatility if requested
    if match_volatility and benchmark is not None:
        bmark_vol = benchmark.std()
        returns = (returns / returns.std()) * bmark_vol
    elif match_volatility:
        raise ValueError("match_volatility requires passing of benchmark.")

    # Handle compound/cumulative calculations
    if compound:
        if cumulative:
            returns = (1 + returns).cumprod() - 1
            if isinstance(benchmark, pd.Series):
                benchmark = (1 + benchmark).cumprod() - 1
        else:
            returns = returns.cumsum()
            if isinstance(benchmark, pd.Series):
                benchmark = benchmark.cumsum()

    # Resample if specified
    if resample:
        returns = returns.resample(resample)
        returns = returns.last() if compound else returns.sum()
        if isinstance(benchmark, pd.Series):
            benchmark = benchmark.resample(resample)
            benchmark = benchmark.last() if compound else benchmark.sum()

    # Create figure
    fig = go.Figure()

    # Add benchmark if provided
    if isinstance(benchmark, pd.Series):
        fig.add_trace(
            go.Scatter(
                x=benchmark.index,
                y=benchmark,
                name=benchmark.name,
                line=dict(color=colors[0], width=line_width),
            )
        )

    # Add returns
    alpha = 0.25 if grayscale else 1
    if isinstance(returns, pd.Series):
        fig.add_trace(
            go.Scatter(
                x=returns.index,
                y=returns,
                #name=returns_label or returns.name,
                showlegend=False,
                line=dict(color=colors[1], width=line_width),
                opacity=alpha,
            )
        )
        if fill:
            fig.add_trace(
                go.Scatter(
                    x=returns.index,
                    y=returns,
                    fill="tozeroy",
                    name=returns_label or returns.name,
                    line=dict(color=colors[1], width=0),
                    opacity=0.25,
                    showlegend=False,
                )
            )
    elif isinstance(returns, pd.DataFrame):
        returns = returns.copy()
        returns.columns = [
            col.replace("return_pct", "回報率") if "return_pct" in col else col
            for col in returns.columns
        ]

        for i, col in enumerate(returns.columns):
            fig.add_trace(
                go.Scatter(
                    x=returns.index,
                    y=returns[col],
                    name=col,
                    line=dict(color=colors[i + 1], width=line_width),
                    opacity=alpha,
                )
            )
            if fill:
                fig.add_trace(
                    go.Scatter(
                        x=returns.index,
                        y=returns[col],
                        fill="tozeroy",
                        name=col,
                        line=dict(color=colors[i + 1], width=0),
                        opacity=0.25,
                        showlegend=False,
                    )
                )

    # Add horizontal lines
    if hline is not None:
        if not isinstance(hline, pd.Series):
            hlcolor = "black" if grayscale else hlcolor
            fig.add_hline(
                y=hline,
                line=dict(color=hlcolor, width=hlw or line_width, dash="dash"),
                name=hllabel,
            )

    # Add zero line
    fig.add_hline(y=0, line=dict(color="gray", width=1))

    # Create subtitle
    if subtitle:
        subtitle_text = f"{returns.index[0].strftime('%Y/%m/%d')} - {returns.index[-1].strftime('%Y/%m/%d')}"
        title = f"{title}<br><sup>{subtitle_text}</sup>"

    # Update layout
    fig.update_layout(
        title=dict(
            text=title,
            font=dict(family=font_family, size=24, color="black"),
            x=0.5,
            xanchor="center",
        ),
        showlegend=True,
        legend=dict(
            x=0.01,
            y=0.99,
            xanchor="left",
            yanchor="top",
        ),
        plot_bgcolor="white",
        paper_bgcolor="white",
        yaxis=dict(
            title=ylabel,
            tickformat=".1%" if percent else "",
            showgrid=True,
            gridcolor="lightgray",
            type="log" if log_scale else "linear",
            zeroline=True,
            zerolinecolor="black",
            zerolinewidth=1,
        ),
        xaxis=dict(
            showgrid=True,
            gridcolor="lightgray",
        ),
        hovermode="x unified",
    )

    if saveFig:
        if isinstance(saveFig, dict):
            fig.write_image(**saveFig)
        else:
            fig.write_image(saveFig, width=1200, height=500)
    # Show or return figure
    if show:
        fig.show()
    return fig


def drawdown_plotly(
    returns,
    grayscale=False,
    font_family="Arial",
    line_width=1,
    log_scale=False,
    match_volatility=False,
    compound=False,
    ylabel="回撤率",
    resample=None,
    subtitle=True,
    saveFig=None,
    show=True,
):
    """
    Plot drawdown using Plotly
    """
    import pandas as pd

    dd = to_drawdown_series(returns)

    fig = plot_timeseries_plotly(
        dd,
        title="回撤圖",
        hline=dd.mean(),
        hlw=2,
        hllabel="Average",
        returns_label="回撤率",
        compound=compound,
        match_volatility=match_volatility,
        log_scale=log_scale,
        resample=resample,
        fill=True,
        line_width=line_width,
        ylabel=ylabel,
        font_family=font_family,
        grayscale=grayscale,
        subtitle=subtitle,
        saveFig=saveFig,
        show=show,
    )

    if not show:
        return fig


# --------------------------- 原本前置處理functions----------------------------------------------
def group_returns(returns, groupby, compounded=False):
    """Summarize returns
    group_returns(df, df.index.year)
    group_returns(df, [df.index.year, df.index.month])
    """
    if compounded:
        return returns.groupby(groupby).apply(comp)
    return returns.groupby(groupby).sum()


def compsum(returns):
    """Calculates rolling compounded returns."""
    # Add 1 to returns, compute cumulative product, and subtract 1 to get compounded returns
    return returns.add(1).cumprod().sub(1)


def remove_outliers(returns, quantile=0.95):
    """Returns series of returns without the outliers."""
    # Remove values greater than the specified quantile to filter out outliers
    return returns[returns < returns.quantile(quantile)]


def comp(returns):
    """Calculates total compounded returns."""
    # Calculate total compounded returns by multiplying all (1 + returns) values and subtracting 1
    return returns.add(1).prod().sub(1)


def to_prices(returns, base=1e5):
    """Converts returns series to price data."""
    # Clean up returns by replacing NaNs and infinite values
    returns = returns.copy().fillna(0).replace([np.inf, -np.inf], np.nan)
    # Convert returns to price data starting from the base value
    return base + base * compsum(returns)


def _prepare_prices(data, base=1.0):
    """Converts return data into prices and performs cleanup."""
    data = data.copy()
    # Convert each column in DataFrame if data is a DataFrame
    if isinstance(data, pd.DataFrame):
        for col in data.columns:
            # Check if column values indicate it is returns data and convert to prices
            if data[col].dropna().min() <= 0 or data[col].dropna().max() < 1:
                data[col] = to_prices(data[col], base)
    # If data is a Series and seems like returns, convert to prices
    elif data.min() < 0 or data.max() < 1:
        data = to_prices(data, base)

    # Clean up data by filling NaNs and replacing infinite values
    if isinstance(data, (pd.DataFrame, pd.Series)):
        data = data.fillna(0).replace([np.inf, -np.inf], np.nan)
    return data


def to_drawdown_series(returns):
    """Convert returns series to drawdown series."""
    # Convert returns to prices to calculate drawdowns
    prices = _prepare_prices(returns)
    # Calculate drawdown as the relative drop from the maximum accumulated value
    drawdown = prices / np.maximum.accumulate(prices) - 1.0
    # Replace negative zeros and infinite values with 0
    return drawdown.replace([np.inf, -np.inf, -0], 0)


def drawdown_details(drawdown):
    """
    Calculates drawdown details, including start, end, and valley dates,
    duration, max drawdown, and 99% max drawdown for every drawdown period.
    """

    def _drawdown_details(drawdown):
        # Identify periods with no drawdown (i.e., drawdown equals zero)
        no_dd = drawdown == 0
        # Identify drawdown start dates (when drawdown switches from 0 to non-zero)
        starts = (~no_dd & no_dd.shift(1)).loc[lambda x: x].index.tolist()
        # Identify drawdown end dates (when drawdown switches from non-zero to 0)
        ends = (
            (no_dd & (~no_dd).shift(1))
            .shift(-1, fill_value=False)
            .loc[lambda x: x]
            .index.tolist()
        )

        # If no drawdown starts are found, return an empty DataFrame
        if not starts:
            return pd.DataFrame(
                columns=[
                    "start",
                    "valley",
                    "end",
                    "days",
                    "max drawdown",
                    "99% max drawdown",
                ]
            )

        # Handle case where drawdown series begins in a drawdown
        if ends and starts[0] > ends[0]:
            starts.insert(0, drawdown.index[0])
        # Handle case where drawdown series ends in a drawdown
        if not ends or starts[-1] > ends[-1]:
            ends.append(drawdown.index[-1])

        # Build the drawdown details DataFrame
        data = []
        for start, end in zip(starts, ends):
            # Extract the drawdown period between start and end
            dd_period = drawdown[start:end]
            # Remove outliers from the drawdown period for 99% max drawdown calculation
            clean_dd = -remove_outliers(-dd_period, 0.99)
            # Append drawdown details including start, valley (minimum), end, duration, and max drawdowns
            data.append(
                (
                    start,
                    dd_period.idxmin(),
                    end,
                    (end - start).days + 1,
                    dd_period.min() * 100,
                    clean_dd.min() * 100,
                )
            )

        # Create DataFrame with drawdown details
        df = pd.DataFrame(
            data,
            columns=[
                "start",
                "valley",
                "end",
                "days",
                "max drawdown",
                "99% max drawdown",
            ],
        )
        # Format start, valley, and end dates as strings
        df[["start", "valley", "end"]] = df[["start", "valley", "end"]].apply(
            lambda x: x.dt.strftime("%Y-%m-%d")
        )
        return df

    # If drawdown is a DataFrame, calculate details for each column
    if isinstance(drawdown, pd.DataFrame):
        return pd.concat(
            {col: _drawdown_details(drawdown[col]) for col in drawdown.columns}, axis=1
        )
    return _drawdown_details(drawdown)


def to_excess_returns(returns, rf, nperiods=None):
    """
    Calculates excess returns by subtracting
    risk-free returns from total returns

    Args:
        * returns (Series, DataFrame): Returns
        * rf (float, Series, DataFrame): Risk-Free rate(s)
        * nperiods (int): Optional. If provided, will convert rf to different
            frequency using deannualize
    Returns:
        * excess_returns (Series, DataFrame): Returns - rf
    """
    if isinstance(rf, int):
        rf = float(rf)

    if not isinstance(rf, float):
        rf = rf[rf.index.isin(returns.index)]

    if nperiods is not None:
        # deannualize
        rf = np.power(1 + rf, 1.0 / nperiods) - 1.0

    return returns - rf


def _prepare_returns(data, rf=0.0, nperiods=None):
    """Converts price data into returns and performs cleanup."""
    data = data.copy()
    # Convert price data into returns if data is a DataFrame
    if isinstance(data, pd.DataFrame):
        for col in data.columns:
            # Check if column values indicate it is price data and convert to returns
            if data[col].dropna().min() >= 0 and data[col].dropna().max() > 1:
                data[col] = data[col].pct_change()
    # If data is a Series and seems like price data, convert to returns
    elif data.min() >= 0 and data.max() > 1:
        data = data.pct_change()

    # Replace infinite values and fill NaNs with 0
    data = data.replace([np.inf, -np.inf], np.nan).fillna(0)

    # Get the calling function's name to determine if excess returns need to be calculated
    function = inspect.stack()[1][3]
    unnecessary_function_calls = [
        "_prepare_benchmark",
        "cagr",
        "gain_to_pain_ratio",
        "rolling_volatility",
    ]
    # Calculate excess returns if the calling function is not in unnecessary_function_calls and rf > 0
    if function not in unnecessary_function_calls and rf > 0:
        return to_excess_returns(data, rf, nperiods)
    return data


def get_longest_dd(returns, period=5):
    returns = _prepare_returns(returns)
    dd = to_drawdown_series(returns.fillna(0))
    dddf = drawdown_details(dd)
    longest_dd = dddf.sort_values(by="days", ascending=False, kind="mergesort")[:period]
    longest_dd = longest_dd.reset_index().drop(["index", "99% max drawdown"], axis=1)
    return longest_dd