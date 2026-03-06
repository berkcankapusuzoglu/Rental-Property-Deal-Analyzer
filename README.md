# Rental Property Deal Analyzer

A web-based tool that helps you evaluate rental property investments. Enter property details (or scrape them from Zillow), and get a full financial breakdown with cash flow projections, rule-of-thumb checks, and optional AI-powered investment analysis.

![Dark-themed single-page app with a 6-step wizard]

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Configure AI (optional)

Copy the example env file and edit it:

```bash
cp .env.example .env
```

**Option A: Free local AI with Ollama (recommended)**

Install [Ollama](https://ollama.com), then:

```bash
ollama pull llama3.2:3b
ollama serve
```

That's it — the app auto-detects Ollama when no Anthropic key is set.

**To use a different model** (e.g. `qwen3.5:4b`):

```bash
# Pull the model first
ollama pull qwen3.5:4b

# Then either set it in .env:
OLLAMA_MODEL=qwen3.5:4b

# Or pass it as an environment variable when running:
OLLAMA_MODEL=qwen3.5:4b python app.py
```

Any model available on Ollama works — just `ollama pull <model>` and set `OLLAMA_MODEL`.

**Option B: Anthropic Claude API (paid)**

Set your API key in `.env`:

```
AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

### 3. Run the app

```bash
python app.py
```

Opens automatically at **http://localhost:8000**. No build step required.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AI_PROVIDER` | `auto` | `auto` (Anthropic if key set, else Ollama), `ollama`, or `anthropic` |
| `ANTHROPIC_API_KEY` | — | Anthropic API key (required for `anthropic` provider) |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `llama3.2:3b` | Any Ollama model name (e.g. `qwen3.5:4b`, `mistral`, `phi3`) |

## How It Works

The app is a **6-step wizard**:

### Step 1: Property Info
Enter the property address, purchase price, type (single-family or multifamily), ARV (after-repair value), and rehab budget. You can also paste a Zillow URL to auto-fill these fields.

### Step 2: Financing
Set your down payment percentage, interest rate, loan term, points, and closing costs. There's a "Cash Purchase" toggle if you're buying without a loan.

### Step 3: Income
Enter monthly rent (with support for multiple units on multifamily properties), other income, and expected annual income growth rate.

### Step 4: Expenses
Enter property taxes, insurance, HOA, utilities, and percentage-based expenses (maintenance, vacancy, CapEx, management). Also set expected annual expense growth.

### Step 5: Review
See a summary of everything before calculating. Go back to any step to adjust.

### Step 6: Results
Full financial dashboard with all metrics, a 5-year projection, amortization schedule, equity growth chart, and optional AI analysis.

## Metrics Explained

### Core Metrics

| Metric | What It Means | Good Target |
|---|---|---|
| **Monthly Cash Flow** | Rent income minus ALL expenses (operating + mortgage). This is money in your pocket each month. | > $100-200/unit |
| **Annual Cash Flow** | Monthly cash flow x 12. | > $1,200/unit |
| **Cash-on-Cash Return (CoC)** | Annual cash flow divided by total cash you invested (down payment + closing costs + rehab + points). Measures the return on YOUR money, not the property's total value. | > 8% |
| **Cap Rate** | Net Operating Income (NOI) divided by purchase price. Measures the property's return independent of financing. Useful for comparing properties regardless of how you finance them. | > 5-6% |
| **NOI (Net Operating Income)** | Annual rental income minus annual operating expenses (taxes, insurance, maintenance, vacancy, CapEx, management, HOA, utilities). Does NOT include mortgage payments. | Positive |
| **DSCR (Debt Service Coverage Ratio)** | NOI divided by annual mortgage payments. Tells you if the property's income covers its debt. Banks typically require 1.25+. Below 1.0 means you're losing money. | > 1.25 |
| **GRM (Gross Rent Multiplier)** | Purchase price divided by annual rent. Lower = better. It's a quick-and-dirty comparison tool — how many years of gross rent to pay off the price. | < 12-15 |
| **Break-Even Occupancy** | The percentage of time the property must be occupied just to cover all expenses + mortgage. Over 85% is risky — too little margin for vacancies. | < 85% |
| **OER (Operating Expense Ratio)** | Total operating expenses as a percentage of gross rental income. Measures expense efficiency. | < 50% |
| **Price / Sqft** | Purchase price divided by square footage. Useful for comparing deals in the same market. | Market-dependent |
| **Rent / Sqft** | Monthly rent divided by square footage. Higher = more efficient income per sqft. | Market-dependent |
| **Cash Flow / Unit** | Monthly cash flow divided by number of units. The classic BiggerPockets metric for multifamily. | > $200/unit |
| **Annual Depreciation** | Building value (price x building %) spread over 27.5 years (IRS residential schedule). A non-cash expense that reduces taxable income. | Informational |
| **After-Tax Cash Flow** | Annual cash flow + annual tax savings from depreciation. Shows your real take-home return. | Positive |

### Total Return — The Four Pillars

Real estate returns come from four sources. The app calculates all four over a 5-year projection:

| Pillar | What It Is |
|---|---|
| **Cash Flow** | Net rental income after all expenses and mortgage payments |
| **Appreciation** | Property value growth over time (based on your growth rate input) |
| **Debt Paydown** | The principal portion of mortgage payments — your tenants are paying down your loan |
| **Tax Benefits** | Annual depreciation deduction x your marginal tax rate = tax savings |

The **5-Year Total Return** is the sum of all four pillars. This gives a much more complete picture than cash flow alone.

### Rule-of-Thumb Checks

| Rule | How It Works | What It Tells You |
|---|---|---|
| **1% Rule** | Monthly rent should be >= 1% of purchase price. A $200K property should rent for $2,000+/month. | Quick filter to see if the numbers could work. |
| **50% Rule** | Operating expenses (excluding mortgage) typically run about 50% of gross rent. If yours are much higher, expenses may eat your cash flow. | Reality check on your expense estimates. |
| **70% Rule** | Purchase price + rehab should be <= 70% of ARV. Only shown when ARV or rehab is entered. Used for flips and BRRRR deals. | Checks if you're paying too much relative to the improved value. |

### Deal Score (Point-Based Scorecard)

The app scores each deal on a point system across 7 key metrics:

| Metric | 2 pts (Strong) | 1 pt (OK) | 0 pts (Weak) |
|---|---|---|---|
| CoC Return | >= 8% | >= 4% | < 4% |
| Cap Rate | >= 6% | >= 4% | < 4% |
| DSCR (skip if cash) | >= 1.25 | >= 1.0 | < 1.0 |
| CF per Unit/mo | >= $200 | >= $100 | < $100 |
| Break-even Occ. | <= 75% | <= 85% | > 85% |
| 1% Rule | pass (2pts) | -- | fail (0pts) |
| 50% Rule | pass (2pts) | -- | fail (0pts) |

**Max: 14 points** (12 if cash purchase, since DSCR is skipped). Verdict thresholds:
- **Great Deal**: >= 75% of max points
- **Borderline Deal**: >= 45% of max points
- **Pass on This Deal**: < 45% of max points

Each factor shows a colored indicator (green/yellow/red) with a one-line explanation of why it scored that way.

### Investment Strategy Guide

| Strategy | Key Metrics | What Makes a Good Deal |
|---|---|---|
| **Cash Flow** | CoC >= 8%, CF/unit >= $200, DSCR >= 1.25 | High rents relative to price, low expenses |
| **Wealth Building** | 5yr Total Return, appreciation, equity growth | Growing markets, value-add potential |
| **Low Risk** | Break-even < 75%, DSCR >= 1.5, 50% rule pass | Conservative margins, strong coverage |
| **BRRRR** | 70% rule pass, ARV spread | Below-market purchase + forced appreciation |

### Which Metrics Matter Most?

1. **Cash flow first** — If monthly cash flow is negative, the deal doesn't work regardless of other metrics
2. **DSCR second** — Must be above 1.0 or you can't service the debt
3. **Break-even occupancy** — Your safety margin against vacancies
4. **CoC return** — The return on YOUR invested capital
5. **Total return** — The full picture including appreciation, debt paydown, and tax benefits

### 5-Year Projection

Projects cash flow, property value, loan balance, equity, and cumulative ROI over 5 years, factoring in your income growth, expense growth, and property value appreciation rates. Cash flows are color-coded: green for positive, red for negative.

### Amortization Schedule

Full year-by-year breakdown of annual payment, principal, interest, remaining balance, and total equity for the entire loan term.

### Equity Growth Chart

Visual bar chart showing how your equity grows over time (years 1, 5, 10, 15, 20, 25, 30) as you pay down the loan and the property appreciates.

### AI Investment Analysis

Click "Run AI Analysis" to get a plain-English assessment from a local LLM (Ollama) or Claude API. The AI reviews all your calculated metrics and provides:
1. Overall assessment
2. Key strengths
3. Key risks
4. Buy/pass recommendation

## Assumptions & Defaults

The calculator makes the following assumptions. Understanding these helps you interpret results accurately.

| Assumption | Default Value | Why |
|---|---|---|
| **Building value %** | 80% of purchase price | IRS requires splitting land vs building for depreciation. 80/20 is standard for residential; condos may be 90/10, rural land-heavy properties 60/40. |
| **Depreciation schedule** | 27.5 years (straight-line) | IRS mandated schedule for residential rental property. Commercial is 39 years. |
| **Marginal tax rate** | 25% | Used to calculate tax savings from depreciation. Adjust to your actual bracket (22-37% for most investors). |
| **Vacancy rate** | 8% of gross rent | National average is 5-8%. Adjust for your local market — urban areas may be lower, rural higher. |
| **Maintenance** | 5% of gross rent | Rule of thumb for ongoing repairs. Older properties (pre-1980) may need 8-10%. |
| **CapEx reserve** | 5% of gross rent | For major replacements (roof, HVAC, appliances). Some investors use 5-10%. |
| **Management fee** | 10% of gross rent | Professional property management cost. Set to 0% if self-managing, but consider your time value. |
| **Closing costs** | 3% of purchase price | Auto-calculated when price is entered. Actual costs vary by state (1-5%). |
| **Insurance** | 0.5% of purchase price/yr | Auto-calculated. Actual costs depend on location, property type, and coverage level. |
| **Property value growth** | 3%/yr | U.S. historical average is ~3-4%/yr. Hot markets may be higher, but don't count on it. |
| **Income growth** | 2%/yr | Rent increases. Conservative estimate; actual depends on lease terms and local market. |
| **Expense growth** | 2%/yr | Inflation on fixed costs (taxes, insurance, utilities). Roughly tracks CPI. |
| **Loan terms** | 30yr fixed, 20% down, 7% rate | Standard conventional investment property loan (2024-2025 rates). |
| **5-year projection** | Includes all 4 return pillars | Cash flow + appreciation + debt paydown + tax savings, compounded with growth rates. |

**What the calculator does NOT account for:**
- Closing costs when selling (typically 6-8% of sale price)
- Capital gains tax on sale
- Depreciation recapture tax (25% on accumulated depreciation when you sell)
- Cost segregation or bonus depreciation (accelerated depreciation strategies)
- Refinancing scenarios
- Rent-ready costs between tenants (cleaning, painting, minor repairs)
- Legal/accounting fees
- Mortgage insurance (PMI) if down payment < 20%

## Example Scenarios

Three realistic scenarios to help you understand what good, mediocre, and bad deals look like. Use these as reference points when evaluating your own deals.

### Good Deal — Cash Flow Rental in a Secondary Market

A well-priced single family home in a secondary market (e.g., Midwest or Southeast) with strong rent-to-price ratio.

| Input | Value |
|---|---|
| Property | 456 Oak Avenue, Arlington VA 22201 |
| Purchase Price | $250,000 |
| ARV | $300,000 |
| Closing Costs | $7,500 |
| Rehab Budget | $15,000 |
| Sqft | 1,800 |
| Value Growth | 3%/yr |
| Down Payment | 20% |
| Interest Rate | 6.5% |
| Loan Term | 30 years |
| Monthly Rent | $2,800 |
| Other Income | $100/mo |
| Income Growth | 2%/yr |
| Property Taxes | $3,000/yr |
| Insurance | $1,500/yr |
| Maintenance | 5% |
| Vacancy | 5% |
| CapEx | 5% |
| Management | 8% |
| Expense Growth | 2%/yr |

**Key Results:**
- Monthly Cash Flow: **$593** | CoC Return: **9.83%** | Cap Rate: **8.92%**
- DSCR: **1.47** | Break-even Occupancy: **74.52%**
- 5-Year Total Return: **$104,189** (143.71% on $72,500 invested)
- Deal Score: **14/14 — Great Deal**
- Strategy Fit: Cash Flow (Strong), Wealth Building (Strong), Low Risk (Moderate)

### Mediocre Deal — Suburban Property with Thin Margins

A typical suburban property where appreciation is the main play. Cash flow is thin but positive.

| Input | Value |
|---|---|
| Property | 220 Maple Dr, Fairfax VA 22030 |
| Purchase Price | $380,000 |
| Closing Costs | $11,400 |
| Rehab Budget | $0 |
| Sqft | 1,500 |
| Value Growth | 3%/yr |
| Down Payment | 20% |
| Interest Rate | 6.75% |
| Loan Term | 30 years |
| Monthly Rent | $2,400 |
| Other Income | $0 |
| Income Growth | 2%/yr |
| Property Taxes | $4,500/yr |
| Insurance | $1,900/yr |
| Maintenance | 5% |
| Vacancy | 5% |
| CapEx | 5% |
| Management | 8% |
| Expense Growth | 2%/yr |

**Key Results:**
- Monthly Cash Flow: **~-$250** | CoC Return: **~-3.5%** | Cap Rate: **~4.7%**
- DSCR: **~0.87** | Break-even Occupancy: **~100%**
- 5-Year Total Return: Positive (appreciation + debt paydown offset negative CF)
- Deal Score: **~4-5/14 — Borderline Deal**
- Strategy Fit: Cash Flow (Poor), Wealth Building (Moderate), Low Risk (Poor)

### Bad Deal — Overpriced Property with Negative Cash Flow

An expensive property in a high-cost area where rent doesn't remotely cover costs.

| Input | Value |
|---|---|
| Property | 789 Expensive Blvd, McLean VA 22101 |
| Purchase Price | $500,000 |
| Closing Costs | $15,000 |
| Rehab Budget | $0 |
| Sqft | 1,200 |
| Value Growth | 3%/yr |
| Down Payment | 20% |
| Interest Rate | 7.5% |
| Loan Term | 30 years |
| Monthly Rent | $2,000 |
| Other Income | $0 |
| Income Growth | 2%/yr |
| Property Taxes | $3,000/yr |
| Insurance | $1,000/yr |
| Maintenance | 5% |
| Vacancy | 8% |
| CapEx | 5% |
| Management | 10% |
| Expense Growth | 2%/yr |

**Key Results:**
- Monthly Cash Flow: **~-$1,690** | CoC Return: **~-17.6%** | Cap Rate: **~2.7%**
- DSCR: **~0.40** | Break-even Occupancy: **~176%** (impossible)
- 5-Year Total Return: $22,029 (only from appreciation + debt paydown, CF deeply negative)
- Deal Score: **2/14 — Pass on This Deal**
- Strategy Fit: Cash Flow (Poor), Wealth Building (Poor), Low Risk (Poor)

**Why it's bad:** The rent ($2,000/mo) is only 0.4% of the purchase price — far below the 1% rule. The mortgage alone ($2,797/mo) exceeds rent. You'd be losing $1,690/month out of pocket. Even with appreciation and tax benefits, the negative cash flow makes this unsustainable.

## Zillow Scraping

The app can attempt to auto-fill property data from a Zillow listing URL. It tries:

1. **httpx** (fast direct HTTP request)
2. **Playwright** (headless Chromium browser, if httpx is blocked)

**Important**: Zillow aggressively blocks automated requests. Scraping may fail with CAPTCHA depending on your network/IP. When it fails, simply enter the property data manually — all fields in Steps 1-4 are editable.

The scraper extracts: address, price, beds, baths, sqft, lot size, year built, property type, Zestimate, rent Zestimate, tax history, HOA fee, description, and a photo.

## Tech Stack

- **Backend**: Python, FastAPI, uvicorn, httpx, BeautifulSoup, Playwright
- **Frontend**: Vanilla HTML/CSS/JS (single file, no frameworks, no build step)
- **AI**: Ollama (local, free) or Anthropic Claude API (cloud, paid)
