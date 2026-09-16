# 2X Graph

The application records BC.Game Crash rounds directly from Chrome's WebSocket
traffic and draws the running 2X graph. Screen-region capture is no longer
required for normal tracking.

## Setup

Install Python 3.10 or newer, Google Chrome, and the dependencies:

```powershell
py -m pip install -r requirements.txt
py app.py
```

Open **Auto Track**, check the Crash URL, and choose **Start Track**. A Chrome
window opens. Complete login, cookie, or region prompts yourself if the site
shows them. Keep that Chrome window open while tracking.

Results are mapped as follows:

- below 2x: `2x-`
- 2x through 9.99x: `2x+`
- 10x or higher: `10x`

ChromeDriver is managed automatically by Selenium. Headless mode is available,
but visible Chrome is recommended when login or site confirmation is needed.

## Browse saved sessions

Choose **History** in the main controls, select a directory containing saved
JSON sessions, and select a file from the list. Double-click it or choose
**Open in New Window** to view that history in a separate complete 2X Graph
window without replacing or stopping the live graph. Historical windows are
read-only and show only the Trend View; live-tracking and editing controls stay
in the main window.

## Telegram alerts

Open **Telegram Alerts** from Auto Track to configure one global bot token and
group/chat ID. Add, update, enable, disable, or remove any number of named alert
rules. Each rule has its own target value, tolerance, and optional forward-step
limit. Existing single-alert settings are migrated automatically.

The app also sends a one-time **10x scarcity** alert when the latest 50 points
contain zero or only one 10x result. The alert resets once that rolling window
contains at least two 10x results.
"# 2x_graph" 
"# 2x_graph" 
