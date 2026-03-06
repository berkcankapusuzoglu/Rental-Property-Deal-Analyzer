"""
Generate 3 example report PDFs by automating the browser.
Run: python generate_examples.py
Requires: playwright (pip install playwright && python -m playwright install chromium)
"""
import asyncio
from playwright.async_api import async_playwright

SCENARIOS = [
    {
        "name": "good_deal",
        "title": "Good Deal — Cash Flow Rental",
        "inputs": {
            "propName": "456 Oak Avenue, Arlington VA 22201",
            "purchasePrice": "250000",
            "arv": "300000",
            "closingCosts": "7500",
            "rehabBudget": "15000",
            "valueGrowth": "3",
            "sqft": "1800",
            "buildingPct": "80",
            "taxRate": "25",
            "downPayment": "20",
            "interestRate": "6.5",
            "loanTerm": "30",
            "points": "0",
            "monthlyRent": "2800",
            "otherIncome": "100",
            "incomeGrowth": "2",
            "propertyTaxes": "3000",
            "insurance": "1500",
            "maintenance": "5",
            "vacancy": "5",
            "capex": "5",
            "management": "8",
            "hoa": "0",
            "utilities": "0",
            "otherExpenses": "0",
            "expenseGrowth": "2",
        },
    },
    {
        "name": "mediocre_deal",
        "title": "Mediocre Deal — Thin Margins",
        "inputs": {
            "propName": "220 Maple Dr, Fairfax VA 22030",
            "purchasePrice": "380000",
            "arv": "",
            "closingCosts": "11400",
            "rehabBudget": "0",
            "valueGrowth": "3",
            "sqft": "1500",
            "buildingPct": "80",
            "taxRate": "25",
            "downPayment": "20",
            "interestRate": "6.75",
            "loanTerm": "30",
            "points": "0",
            "monthlyRent": "2400",
            "otherIncome": "0",
            "incomeGrowth": "2",
            "propertyTaxes": "4500",
            "insurance": "1900",
            "maintenance": "5",
            "vacancy": "5",
            "capex": "5",
            "management": "8",
            "hoa": "0",
            "utilities": "0",
            "otherExpenses": "0",
            "expenseGrowth": "2",
        },
    },
    {
        "name": "bad_deal",
        "title": "Bad Deal — Negative Cash Flow",
        "inputs": {
            "propName": "789 Expensive Blvd, McLean VA 22101",
            "purchasePrice": "500000",
            "arv": "",
            "closingCosts": "15000",
            "rehabBudget": "0",
            "valueGrowth": "3",
            "sqft": "1200",
            "buildingPct": "80",
            "taxRate": "25",
            "downPayment": "20",
            "interestRate": "7.5",
            "loanTerm": "30",
            "points": "0",
            "monthlyRent": "2000",
            "otherIncome": "0",
            "incomeGrowth": "2",
            "propertyTaxes": "3000",
            "insurance": "1000",
            "maintenance": "5",
            "vacancy": "8",
            "capex": "5",
            "management": "10",
            "hoa": "0",
            "utilities": "0",
            "otherExpenses": "0",
            "expenseGrowth": "2",
        },
    },
]


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        for scenario in SCENARIOS:
            print(f"Generating: {scenario['title']}...")
            page = await browser.new_page()
            await page.goto("http://localhost:8000", wait_until="networkidle")

            # Fill all inputs
            for field_id, value in scenario["inputs"].items():
                await page.evaluate(
                    f"document.getElementById('{field_id}').value = '{value}'"
                )

            # Navigate to results
            await page.evaluate("goToStep(6)")
            await page.wait_for_timeout(500)

            # Save as PDF
            pdf_path = f"examples/{scenario['name']}_report.pdf"
            await page.pdf(
                path=pdf_path,
                format="Letter",
                print_background=True,
                margin={"top": "0.5in", "bottom": "0.5in", "left": "0.4in", "right": "0.4in"},
            )
            print(f"  Saved: {pdf_path}")
            await page.close()

        await browser.close()
        print("\nDone! All 3 example reports saved to examples/ folder.")


if __name__ == "__main__":
    asyncio.run(main())
