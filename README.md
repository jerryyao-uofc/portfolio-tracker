# Portfolio Overview

A local dashboard for a list of stocks, ETFs, and crypto. It reads CSV files on your computer, loads recent market prices, and shows how the portfolio is put together: value over time, allocation, industries, news, a simple stress case, and a long-range illustration in today's purchasing power.

The repository includes a **fictional sample** sized to about **$1,000,000** using market prices from late October 2026. Those share counts are not a recommendation, and the live total will drift as prices move. Replace `data/holdings.csv` with your own file when you run it for yourself. Nothing in this repo is a real person's portfolio.

## What you need

- macOS, Linux, or Windows
- Python 3.10 or newer
- An internet connection, so the app can read prices, exchange rates, inflation, and headlines

```bash
python3 -m pip install -r requirements.txt
```

`certifi` is the only dependency. The site itself is served by Python's standard library.

## Start the dashboard

From this folder:

```bash
python3 app.py
```

Then open [http://127.0.0.1:8765/](http://127.0.0.1:8765/).

The process keeps running until you stop it with Ctrl+C. Closing the browser tab does not stop it. The site is only on your machine (`127.0.0.1`). It does not have accounts, and it does not upload your CSV.

On a Mac you can instead double-click **Portfolio Tracker.app** in this folder. The first time, if macOS says the app cannot be verified, right-click it, choose **Open**, and confirm. Leave the app inside this folder so it can find `app.py` and `data/`. If it fails to start, read `tracker.log`.

## Use your own holdings

1. Copy `data/holdings.csv` or replace it.
2. Keep the header row.
3. Add one row per position. Extra CSV files in `data/` are added together. If the same symbol appears twice, the shares are summed.
4. Save the file and click **Refresh prices** in the dashboard.

| Column | Required | What to put |
| --- | --- | --- |
| `asset_type` | Yes | `stock` or `crypto`. ETFs are entered as `stock`; the price feed marks them as ETFs. |
| `symbol` | Yes | Ticker, such as `AAPL`, `VOO`, or `BTC`. Crypto is priced in US dollars (`BTC` becomes `BTC-USD`). |
| `shares` | Yes | How many shares or coins you hold. Decimals are fine. |
| `cost_per_share` | No | Optional average cost. The current dashboard does not display it. |

Example:

```csv
asset_type,symbol,shares
stock,AAPL,10
stock,VOO,25
crypto,ETH,1.5
```

The sample file is a mix of US stocks, US and international ETFs, a bond ETF, a gold trust, bitcoin, and ether. The weights were chosen so the position values added up to about one million US dollars at the prices used to build the file.

## Read the dashboard

Amounts start in US dollars. At the top right, switch to **SGD**, **CAD**, or **RMB**. Those buttons use the latest exchange rate against the US dollar and show that rate under the total. The five summary cards shrink their type if a currency's figures are long, and the value chart widens its vertical axis so the labels still fit.

1. **Summary.** Total value, today's move, the change over the selected window, the number of holdings, and estimated dividend income. Income is the recent annual dividend of the stocks and ETFs, divided by 12. Crypto is left out of that figure.
2. **Value.** Combined market value for the past week, past month, or three months. Green means that window is up. Red means it is down.
3. **Drawdown risk.** A simple stress case: stocks and stock ETFs down 20 percent, crypto down 50 percent, gold unchanged. It is one portfolio total, not a drop for each company.
4. **Allocation.** Rectangle map. Area is the holding's weight. Color is today's move.
5. **ETF and industry.** Three pies: ETF versus everything else, ETFs by region (United States, developed markets, developing markets, and other), and industries for the holdings that are not ETFs. Hover a slice for its dollar value and share of that pie.
6. **In the news.** Headlines for the individual stocks, from Yahoo Finance. The row advances on its own, pauses when you hover, and the arrows step through it.
7. **Holdings.** Symbol, type, industry or ETF region, shares, price, today's move, value, and weight.
8. **Look-through, including shares inside ETFs.** Opens each ETF with the fund's published top holdings and adds those to any shares you hold directly. The dark part of a bar is held directly. The orange part is inside an ETF. The rest of each fund, beyond the published top holdings, stays in **Other**.
9. **Ten, twenty, and thirty years.** An illustration of the whole portfolio, not a forecast for each company. Each holding is compounded from its own annualized price change over the last five years, pulled slightly toward 6 percent a year, and limited to between −10 and +12 percent a year. Holdings with less than a year of history use 6 percent. Average consumer-price inflation over the last 10 years is then removed, using the inflation rate for the currency you selected, so the result is in today's purchasing power.

Prices, rates, and headlines are cached for about a minute. **Refresh prices** loads them again.

## Where the numbers come from

- Prices, ETF holdings, dividends, and headlines: Yahoo Finance
- Exchange rates: Yahoo Finance (`SGD=X`, `CAD=X`, `CNY=X`), quoted as units of foreign currency per US dollar
- Inflation: World Bank consumer-price series for the United States, Singapore, Canada, and China

## Project layout

```text
app.py                 local server and data loading
static/index.html      the dashboard
data/holdings.csv      sample portfolio
start-tracker.sh       used by the Mac app
requirements.txt       Python dependency
```

## Notes

This is a personal learning and demonstration project. Market data can be missing, delayed, or wrong. The stress case, the look-through, and the long-range view are illustrations. They are not advice and they are not a prediction of what these assets will do.
