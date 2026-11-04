# Posting

A live log of new LinkedIn jobs for **Corporate Development**, **Strategic Finance** and **Investment Associate** roles in **San Francisco, Chicago and New York**.

GitHub runs the check every 15 minutes for free and hosts the page, so it works on any device, even when your computer is off.

## Where it lives

- Page: **https://a-gotanco.github.io/postings/**
- Schedule: `.github/workflows/posting.yml` (every 15 min; run it by hand from **Actions → Check LinkedIn → Run workflow**)
- Data: `jobs.json`, which the workflow updates after each check

## Good to know

- **Timing.** GitHub often starts scheduled runs late, so "every 15 minutes" can sometimes be 20 to 30.
- **Public page.** Anyone with the link can see the page. It only shows public job listings.
- **If a check fails.** LinkedIn sometimes blocks requests from GitHub's servers. The run then shows a red ✗ in the Actions tab, GitHub emails you, and the next run tries again. If every run keeps failing, the fallback is to run it from your own computer (below).
- **The log keeps 30 days of jobs** (`KEEP_DAYS` in posting.py).

## Change what it searches

Edit `posting.py` on GitHub (click the file, then the pencil icon). The settings are at the top:
- `CATEGORIES`: the search terms, plus a title filter so only jobs with the role in the *title* are kept
- `METROS`: the cities

To change how often it runs, edit the `cron` line in `.github/workflows/posting.yml`.

## Run it on your own computer instead (optional)

```
python3 posting.py
```
Then open http://localhost:8787. It checks every 15 minutes for as long as the Terminal window stays open.
