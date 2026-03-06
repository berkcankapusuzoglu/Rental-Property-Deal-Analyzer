# Contributing

Thanks for your interest in contributing to the Rental Property Deal Analyzer!

## Running Locally

```bash
pip install -r requirements.txt
python -m playwright install chromium
python app.py
```

The app opens at [http://localhost:8000](http://localhost:8000).

## Submitting Issues

- **Bugs:** Use the [Bug Report](.github/ISSUE_TEMPLATE/bug_report.md) template
- **Features:** Use the [Feature Request](.github/ISSUE_TEMPLATE/feature_request.md) template

## Submitting Pull Requests

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Test manually in the browser (there is no automated test suite)
4. Open a PR with a clear description of the change

## Code Style

- **Python:** PEP 8, minimal dependencies
- **JavaScript:** Vanilla JS inside an IIFE, use `$()` helper for `getElementById`
- **CSS:** Dark theme via `:root` custom properties, light theme in `@media print`
- **Commits:** Conventional commits (`feat:`, `fix:`, `docs:`, etc.)
- **Architecture:** Keep it simple — `app.py` + `index.html` carry all logic
