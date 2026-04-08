# Contributing

Thanks for taking the time to contribute! 🎉

## Ways to help

- **Report a bug** — open an [Issue](../../issues) with steps to reproduce
- **Request a feature** — open an Issue describing the use case
- **Submit a fix or improvement** — open a Pull Request

## Development setup

```bash
git clone https://github.com/YOUR_USERNAME/teslamate-supercharger-costs
cd teslamate-supercharger-costs

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Testing without a live Tesla account

Save a real API response to a JSON file and use `--input`:

```bash
# Test against a saved response (no Tesla API call, no DB writes)
python importer.py --input sample_history.json --dry-run
```

## Pull Request checklist

- [ ] Code is in English
- [ ] Secrets / credentials are never hardcoded
- [ ] New env vars are documented in `.env.example`
- [ ] `README.md` is updated if behaviour changes
- [ ] Tested with `--dry-run` before merging

## Questions?

Open an Issue or start a Discussion — the community is friendly.
