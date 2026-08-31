# philterd-docs

Builds `docs.philterd.ai` from the per-product MkDocs sites.

Each product keeps its own `docs/` and its own `mkdocs.yml` in its own repo. This
repo clones them, checks out the release tags named in `products.yaml`, builds each
with a shared header, and assembles one site. Nothing is copied here by hand and no
product repo needs a write credential, because the flow pulls rather than pushes.

## Running it

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python build.py                     # clone from GitHub
.venv/bin/python build.py --source-root ..    # build from sibling clones instead
.venv/bin/python build.py --only phileas      # one product
.venv/bin/python build.py --force             # rebuild versions already published
```

Output lands in `site/`. Serve it with `python3 -m http.server -d site` to check
links between products.

A local source is cloned into `.work/` rather than used in place, so checking out
release tags never touches the working tree you develop in.

## What `products.yaml` decides

`versions: latest` builds only the newest release tag and serves it at `/<path>/`.
`versions: all` builds every release tag at or above `min_version`, serves each at
`/<path>/<version>/`, and publishes the newest again at `/<path>/latest/`.

The split follows one rule: things you deploy and run get versioned docs, because a
customer running an older release needs docs matching their build. Things you depend
on as a library get latest only, because upgrading is a coordinate bump. Philter is
versioned, Phileas is not.

`dev: true` additionally publishes the default branch at `/<path>/dev/`. Without it,
a docs change merged to `main` appears nowhere until the next release, which leaves no
way to review docs work on the site before shipping it. The dev copy is always
`noindex`, because it describes behavior that is not released yet.

`min_version` is a real lever, not a formality. Every version published here is a
version people will open support requests about.

## Design notes

**Cloning is avoided when there is nothing to build.** Release tags are listed with
`git ls-remote` before any clone happens. A nightly run that finds no new tags costs
two ls-remote calls and clones nothing, which matters because the histories are 90M
(Philter) and 47M (Phileas). When there is work, an existing `.work/` clone is reused
with a fetch, and a fresh remote clone is blobless so the file contents of releases
that will never be built are not downloaded.

**Published versions come from tags, not `main`.** Docs describe a release, so a
build from `main` would document unreleased behavior. Each version is built from its
own tag. The `dev: true` copy is the exception and is kept out of the index for
exactly that reason.

**One failure does not take down the site.** Products and versions build
independently. A tag that fails leaves its previously published copy in place, and
the run reports the failure at the end with a non-zero exit. Philter tag 2.6.0
predates the `docs/` directory and is skipped rather than reported as a failure.

**Only `latest` is indexable.** Publishing eight versions of every page means eight
copies competing in search. Each numbered version is self-canonical and carries
`noindex`; `/<product>/latest/` is the indexed copy. The newest tag is therefore
built twice rather than copied, so `latest` canonicalizes to itself instead of to a
`noindex` page.

**`mike` is not used.** It manages a `gh-pages` branch and writes a `versions.json`,
and this script already does the assembly. The version dropdown itself is an
mkdocs-material theme feature switched on by `extra.version.provider`, which reads
whatever `versions.json` is published. It renders with `mike` never installed.

**Product configs are layered, not edited.** `build.py` writes a small
`mkdocs.philterd.yml` beside each product's own `mkdocs.yml` using MkDocs `INHERIT`.
It sets `site_url`, for correct canonicals and sitemap, and `theme.custom_dir` for
the shared header. Everything else stays owned by the product repo. The generated
file sits in the same directory as its parent so relative paths such as `docs_dir`
resolve identically.

The shared header in `overrides/main.html` uses Material's `announce` block, a
documented extension point, so it survives theme upgrades. A product that already
sets its own `theme.custom_dir` (PhEye does) will need its overrides merged before
it can be added here.

## Before this goes live

- [ ] Remove `extra.version` and the `mike` requirement from `phileas/docs/mkdocs.yml`.
      Material emits the provider into every page's client config and the theme then
      fetches `versions.json` at the site root. With Phileas published latest-only
      there is no such file, so that is a 404 on every page load.
- [ ] Confirm whether Philter 4.0.0 shipped. `mkdocs.yml` sets `philter_version: 4.0.0`
      but the newest tag is 3.4.0, so both the existing per-repo workflow and this
      build resolve `latest` to 3.4.0.
- [ ] Point `docs.philterd.ai` at this repo's GitHub Pages site and set the Pages
      source to GitHub Actions.
- [ ] Keep the per-repo `docs.yml` workflows publishing for a transition window, with
      their `site_url` changed to the `docs.philterd.ai` address so the old
      `philterd.github.io` pages emit canonicals pointing at the new home. Retire them
      once the new URLs have taken over. Turning them off immediately would 404 URLs
      that are indexed today.
- [ ] Replace the deploy step in each product's `docs.yml` with a build-only job, so a
      docs change is still validated by that repo's own CI on pull requests.
- [ ] Update the `docs:` links in `philterd-website/data/site.yaml`.

## How a build gets triggered

- **Nightly cron.** A release published during the day is live the next morning, with
  no wiring in any product repo.
- **Push to `main` here.** Changes to the build script, the product list, the landing
  page, or the shared header rebuild and redeploy.
- **`workflow_dispatch`.** To pick up a release without waiting for the nightly run.

Most runs do nothing. Tags and branch tips are checked with `git ls-remote` before
anything is cloned, so a night with no new releases and no new commits finishes in
under a second and republishes an identical site.

Two things to know about the schedule. GitHub disables scheduled workflows in a
repository after 60 days without commits, so if this repo goes quiet, confirm the
nightly is still enabled. And Actions cron is best-effort, sometimes delayed by up to
an hour.

## Why the build pulls rather than each repo pushing

Having each product repo build its docs and push them here would need a
content-write credential in every product repo, and any one of them being compromised
means arbitrary pages published on `docs.philterd.ai`. It would also split the build
across fourteen environments, so the shared header and the `noindex` policy would have
to be applied correctly in all of them rather than in one place.

The one real argument for the push model is fidelity: docs pushed at release time were
built with the dependencies current at that release, whereas this rebuilds Philter
2.7.0's docs with today's mkdocs-material. If an old tag ever stops building, pin the
versions in `requirements.txt` rather than moving to push.

## Adding a product

Add an entry to `products.yaml` and a card to `landing/index.html`. The product needs
a `docs/mkdocs.yml` and at least one `X.Y.Z` release tag. Check first that its build
does not depend on anything outside `requirements.txt`.
