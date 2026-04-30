# zebra-warrantycheck

A small Playwright-driven CLI that batch-checks Zebra device warranty status by
driving [https://support.zebra.com/warrantycheck](https://support.zebra.com/warrantycheck)
in a real browser, intercepting the underlying API call, and writing the raw
JSON responses to a file.

## What it's for

The Zebra warranty page only lets you look up one serial number at a time, and
its UI shows a trimmed-down view of what the backend actually returns. This
script:

- Reads a CSV with one or more serial numbers.
- Submits each one through the warranty page (handling the cookie banner and
  invisible reCAPTCHA along the way).
- Captures the full JSON response from Zebra's internal Apex endpoint —
  including all service contracts, entitlement dates, distributor/reseller
  info, end-of-service-life, etc., not just the fields rendered in the UI.
- Writes the combined results to a single JSON file.

## Requirements

- Python 3.13+
- [`uv`](https://github.com/astral-sh/uv) for environment + dependency
  management (already configured in `pyproject.toml`).
- Playwright with the Chromium browser installed.

The repo already has `playwright` as a dependency and Chromium downloaded into
`.venv`. If starting from a fresh clone:

```bash
uv sync
uv run playwright install chromium
```

## How to run

Prepare a CSV with serial numbers in the first column. The header row is
optional — anything that contains the word "serial" in the first cell of row 0
is treated as a header and skipped. Example (`serials.csv`):

```csv
serial_number
12345678901234
```

Then run:

```bash
uv run check_warranty.py serials.csv -o results.json
```

A Chromium window will open, accept the OneTrust cookie banner, and submit each
serial in turn. Progress is printed to stderr; the JSON results land in
`results.json`.

### Flags

| Flag                  | Default                  | Purpose                                                                                                  |
| --------------------- | ------------------------ | -------------------------------------------------------------------------------------------------------- |
| `-o`, `--output PATH` | `warranty_results.json`  | Path to write the combined JSON output to.                                                               |
| `--timeout MS`        | `30000`                  | Per-serial wait for the warranty API response, in milliseconds.                                          |
| `--headless`          | off (i.e. headful)       | Run the browser without a visible window. Tends to fail reCAPTCHA — see notes below. Use at your own risk. |

### Output shape

The output is a JSON array, one entry per input serial:

```json
[
  {
    "serial_number": "123456xxxx",
    "response": {
      "numOfRecords": "1",
      "message": "",
      "serialNumberDetails": {
        "asset": [
          {
            "serialNumber": "123456xxx",
            "product": "ET45CB-101D2B0-A6",
            "description": "ET45, 10\", 5G, WIFI6, SE4710, 8GB/128GB, ANDROID GMS, ROW SKU",
            "ownerAccount": "xxx",
            "eOSLDate": "12/29/2029 06:00:00",
            "serviceDetails": {
              "serviceDetail": [
                {
                  "contractNumber": "123456",
                  "contractStartDate": "02/28/2026 18:00:00",
                  "contractEndDate": "02/27/2031 18:00:00",
                  "entitlementName": "EMEA-Z1-SSE-COMP-DBD - 1",
                  "supportProgDesc": "Comprehensive Coverage"
                }
              ]
            }
          }
        ]
      }
    }
  }
]
```

On failure, the entry contains an `error` field instead of (or alongside) the
`response`:

```json
{ "serial_number": "...", "error": "timeout after 30000 ms waiting for serialCalloutEVM response" }
```

## Key findings about the Zebra warranty site

A short tour of what the script has to deal with, since it's not obvious from
the page source:

### 1. The form is in Salesforce LWC Shadow DOM

The warranty page is built with Salesforce Lightning Web Components. The serial
input and Search button are encapsulated inside multiple shadow roots, so plain
`document.querySelectorAll('input,textarea')` returns nothing useful — that
fooled the first scouting pass.

Playwright locators pierce open shadow roots automatically, so the script just
uses simple selectors:

- `input[placeholder='Serial Number']`
- `button:has-text('Search')`

No special shadow-piercing syntax is needed.

### 2. The data comes from one specific Apex call

When you click Search, the page fires roughly a dozen requests against
`POST /webruntime/api/apex/execute`. They're all the same URL — the actual
operation lives in the **POST body's `method` field**. Most are page-chrome
noise (`getMenuVisibility`, `fetchCurrentUserDetails`, `getCaptchaSiteKeys`,
`warrantyCheckAuditLog`, …).

The one we want is:

```json
{
  "namespace": "",
  "classname": "@udd/01pKk000000L6Wn",
  "method": "serialCalloutEVM",
  "params": { "serialNumber": "...", "captchaToken": "..." },
  "isContinuation": false,
  "cacheable": false
}
```

The script's response listener filters by both the URL path and the presence of
`serialCalloutEVM` in the request body, so it ignores the surrounding noise and
hands back exactly the warranty payload.

The response body shape is `{ "returnValue": { ... }, "cacheable": false }` —
the script unwraps `returnValue` into the `response` field of each output
entry.

### 3. Invisible reCAPTCHA and headless mode

The page uses Google reCAPTCHA in **invisible** mode (`size=invisible`). When
you click Search, it scores the session in the background and produces a token
that's included in the `serialCalloutEVM` POST.

In **headless Chromium**, reCAPTCHA's bot-detection scores low, the call comes
back **HTTP 400 — `The Apex request is invalid.`**, and the page shows
"Error fetching warranty details." In headful mode, with a realistic User-Agent
and `--disable-blink-features=AutomationControlled`, it scores high enough to
pass.

That's why the script defaults to headful. `--headless` is exposed for
completeness but is expected to fail unless Zebra changes their captcha setup.

### 4. The OneTrust cookie banner is a click-blocking overlay

The OneTrust consent SDK (`#onetrust-consent-sdk`) injects a full-page dark
filter (`.onetrust-pc-dark-filter`) that intercepts pointer events until you
accept or reject. If the script just clicked Accept and immediately moved on,
subsequent clicks on the Search button would silently fail because the overlay
was still fading out.

The script:

1. Waits up to 10s for `#onetrust-accept-btn-handler` to appear.
2. Clicks it.
3. Waits for `.onetrust-pc-dark-filter` to become hidden before doing anything
   else.

Once accepted, OneTrust persists the consent in cookies, so reloading between
serials doesn't bring the banner back.

### 5. Resetting between serials

After a successful lookup, the page replaces the form area with the result. The
simplest reliable reset is to navigate back to the warranty URL, which clears
the result state without re-triggering the cookie banner (cookies persist for
the whole browser context). The script does that between every pair of
serials.

## Files

- `check_warranty.py` — the CLI script.
- `serials.csv` — example input.
- `results.json` — example output (regenerated each run).
- `pyproject.toml` / `uv.lock` — dependency manifest.
