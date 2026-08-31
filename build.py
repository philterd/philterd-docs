#!/usr/bin/env python3
"""Build docs.philterd.ai from the per-product MkDocs sites.

Each product keeps its own docs/ and its own mkdocs.yml in its own repo. This
script clones each repo, checks out the release tags named by products.yaml,
builds them with a shared theme, and assembles one site.

  ./build.py                          # clone from GitHub, incremental
  ./build.py --source-root ..         # build from sibling clones instead
  ./build.py --only phileas           # one product
  ./build.py --force                  # rebuild versions already present

A product or version that fails to build does not stop the run. The previously
published copy is left in place and the failure is reported at the end, so one
broken tag cannot take the whole site down.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.resolve()
SITE = ROOT / "site"
WORK = ROOT / ".work"
# Build state, deliberately outside SITE so it is never published.
MANIFEST = ROOT / ".build-manifest.json"
TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def detail(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        return ((exc.stderr or exc.stdout or "").strip() or str(exc))[-400:]
    return str(exc)


def parse_version(tag):
    m = TAG_RE.match(tag)
    return tuple(int(g) for g in m.groups()) if m else None


def sort_tags(names):
    """Release tags, oldest first. Anything that is not X.Y.Z is ignored."""
    tags = {(parse_version(t), t) for t in names if parse_version(t)}
    return [t for _, t in sorted(tags, key=lambda x: x[0])]


def source_url(product, source_root):
    if source_root:
        src = (Path(source_root) / product["repo"]).resolve()
        if not (src / ".git").exists():
            raise RuntimeError(f"no git clone at {src}")
        return str(src)
    return f"https://github.com/philterd/{product['repo']}.git"


def remote_head(product, source_root):
    """Commit at the tip of the default branch, without cloning."""
    out = run(["git", "ls-remote", source_url(product, source_root), "HEAD"]).stdout
    return out.split()[0] if out.split() else None


def default_branch(repo_dir):
    run(["git", "-C", str(repo_dir), "remote", "set-head", "origin", "--auto"])
    return run(["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref",
                "origin/HEAD"]).stdout.strip()


def remote_tags(product, source_root):
    """List release tags over the wire, without cloning.

    A nightly run usually finds nothing new. Asking for the tag list first means
    the common case costs one ls-remote instead of a full clone of a repo whose
    history runs to tens of megabytes.
    """
    out = run(["git", "ls-remote", "--tags", source_url(product, source_root)]).stdout
    names = []
    for line in out.splitlines():
        ref = line.split("refs/tags/", 1)[-1].strip()
        names.append(ref[:-3] if ref.endswith("^{}") else ref)
    return sort_tags(names)


def fetch(product, source_root):
    """Put a throwaway clone of the product repo in .work/<repo>.

    Reused across runs when it survives, so a build that does have work to do
    fetches only what is new. A local source is still cloned rather than used in
    place, so checking out tags never touches the working tree you develop in.
    """
    dest = WORK / product["repo"]
    src = source_url(product, source_root)
    if (dest / ".git").exists():
        run(["git", "-C", str(dest), "remote", "set-url", "origin", src])
        run(["git", "-C", str(dest), "fetch", "--tags", "--prune", "--quiet", "origin"])
    else:
        cmd = ["git", "clone", "--quiet"]
        if not source_root:
            # Blobless clone: file contents are fetched on demand at checkout, so
            # the blobs of every historical release are never downloaded.
            cmd.append("--filter=blob:none")
        run(cmd + [src, str(dest)])
    return dest


def has_docs(repo_dir, ref=None):
    """Whether docs/mkdocs.yml exists, at a ref or in the working tree.

    Early tags of a product can predate its docs/ directory entirely, and a repo
    can be listed here before it has any docs at all. Neither is an error, but
    both have to be found before a build is attempted rather than as a stack
    trace from inside one.
    """
    if ref is None:
        return (repo_dir / "docs" / "mkdocs.yml").exists()
    return subprocess.run(
        ["git", "-C", str(repo_dir), "cat-file", "-e", f"{ref}:docs/mkdocs.yml"],
        capture_output=True,
    ).returncode == 0


def built(state, tag):
    """Legacy manifests stored True; current ones store a status string."""
    return state.get(tag) in (True, "built")


def warn_stale_version_provider(repo_dir, name):
    """A latest-only product must not declare a version provider.

    Material emits the provider into every page's client config and the theme
    then fetches versions.json at the site root. With no versions.json published
    that is a 404 on every page load, and it arms the "outdated version" banner.
    Fix it by deleting extra.version from the product's own mkdocs.yml.
    """
    config = repo_dir / "docs" / "mkdocs.yml"
    if not config.exists():
        return
    text = config.read_text(encoding="utf-8", errors="ignore")
    if re.search(r"provider:\s*mike", text):
        print(f"    WARNING: {name} is latest-only but declares extra.version.provider;"
              " remove it from its mkdocs.yml")


def write_config(repo_dir, site_url):
    """Generate a build config beside the product's own mkdocs.yml.

    INHERIT deep-merges onto the product's config, so the product keeps full
    ownership of its nav, plugins, and theme. Only site_url (for correct
    canonicals and sitemap) and the shared header are layered on. The generated
    file sits in the same directory as its parent so relative paths such as
    docs_dir resolve identically either way.
    """
    parent = repo_dir / "docs" / "mkdocs.yml"
    if not parent.exists():
        raise RuntimeError("no docs/mkdocs.yml")
    cfg = repo_dir / "docs" / "mkdocs.philterd.yml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "INHERIT": "./mkdocs.yml",
                "site_url": site_url,
                "theme": {"custom_dir": str(ROOT / "overrides")},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return cfg


def build(repo_dir, out_dir, site_url, mkdocs):
    cfg = write_config(repo_dir, site_url)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    run([mkdocs, "build", "-f", str(cfg), "-d", str(out_dir)])


def noindex(out_dir):
    """Keep superseded versions out of the index.

    Publishing nine versions of every page means nine copies competing in search.
    Only the copy at /<product>/latest/ is indexable; each numbered version is
    self-canonical and noindex, so nothing points search engines at a page that
    is not the current one.
    """
    for html in out_dir.rglob("*.html"):
        text = html.read_text(encoding="utf-8", errors="ignore")
        if "<head>" in text and 'name="robots"' not in text:
            html.write_text(
                text.replace("<head>", '<head><meta name="robots" content="noindex">', 1),
                encoding="utf-8",
            )


def write_versions_json(dest, tags_desc, newest):
    (dest / "versions.json").write_text(
        json.dumps(
            [
                {"version": t, "title": t, "aliases": ["latest"] if t == newest else []}
                for t in tags_desc
            ],
            indent=2,
        ),
        encoding="utf-8",
    )


def write_redirect(path, target, canonical):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="refresh" content="0; url={target}">\n'
        f'<link rel="canonical" href="{canonical}">\n'
        '<meta name="robots" content="noindex">\n'
        "</head>\n<body>\n"
        f'Redirecting to <a href="{target}">the current documentation</a>.\n'
        "</body>\n</html>\n",
        encoding="utf-8",
    )


def build_dev(product, repo_dir, dest, base, args, state, failures):
    """Publish the default branch at /<product>/dev/.

    Docs merged to main would otherwise not appear anywhere until the next
    release, which leaves no way to review docs work before shipping it. This
    copy is always noindex: it describes behavior that is not released yet.
    """
    name = product["name"]
    try:
        repo_dir = repo_dir or fetch(product, args.source_root)
        run(["git", "-C", str(repo_dir), "checkout", "--quiet", "--force",
             "--detach", default_branch(repo_dir)])
        if not has_docs(repo_dir):
            print("    skipped dev (no docs/mkdocs.yml on the default branch)")
            return
        build(repo_dir, dest / "dev", f"{base}/dev/", args.mkdocs)
        noindex(dest / "dev")
        state["__dev__"] = remote_head(product, args.source_root)
        print("    built dev")
    except Exception as e:
        failures.append(f"{name} dev: {detail(e)}")
        print("    FAILED dev")


def do_product(product, args, manifest, mkdocs, failures):
    name, path = product["name"], product["path"]
    base = f"{args.site_url}/{path}"
    dest = SITE / path
    state = manifest.setdefault(path, {})
    tags = remote_tags(product, args.source_root)
    if not tags:
        raise RuntimeError("no release tags")

    want_dev = bool(product.get("dev"))
    head = remote_head(product, args.source_root) if want_dev else None

    if product["versions"] == "latest":
        # Compared against the newest tag on the remote, not the one finally built,
        # so a newest tag that carries no docs does not force a clone every night.
        seen = tags[-1]
        fresh = state.get("seen") == seen and dest.exists()
        dev_fresh = not want_dev or (state.get("__dev__") == head and (dest / "dev").exists())
        if fresh and dev_fresh and not args.force:
            print(f"  {name}: latest = {state.get('latest')}")
            print("    up to date, skipping")
            return

        repo_dir = fetch(product, args.source_root)
        with_docs = [t for t in tags if has_docs(repo_dir, t)]
        if not with_docs:
            raise RuntimeError("no release tag contains docs/mkdocs.yml")
        newest = with_docs[-1]
        print(f"  {name}: latest = {newest}")
        if newest != seen:
            print(f"    note: {seen} has no docs/mkdocs.yml, using {newest}")

        if state.get("latest") != newest or not dest.exists() or args.force:
            run(["git", "-C", str(repo_dir), "checkout", "--quiet", "--force", newest])
            warn_stale_version_provider(repo_dir, name)
            # Rebuilding the product root removes anything nested under it, dev included.
            build(repo_dir, dest, f"{base}/", mkdocs)
            state["latest"] = newest
            state.pop("__dev__", None)
        state["seen"] = seen
        if want_dev:
            build_dev(product, repo_dir, dest, base, args, state, failures)
        return

    floor = parse_version(product.get("min_version", "0.0.0"))
    wanted = [t for t in tags if parse_version(t) >= floor]
    if not wanted:
        raise RuntimeError(f"no release tags at or above {product.get('min_version')}")
    newest = wanted[-1]
    print(f"  {name}: {len(wanted)} version(s), latest = {newest}")

    todo = [t for t in wanted
            if state.get(t) != "nodocs" and not (built(state, t) and (dest / t).exists())]
    need_latest = state.get("__latest_seen__") != newest or not (dest / "latest").exists()
    need_dev = want_dev and (state.get("__dev__") != head or not (dest / "dev").exists())
    if not todo and not need_latest and not need_dev and not args.force:
        print("    up to date, skipping")
        return

    repo_dir = fetch(product, args.source_root)
    for tag in wanted:
        if state.get(tag) == "nodocs" and not args.force:
            continue
        if built(state, tag) and (dest / tag).exists() and not args.force:
            continue
        if not has_docs(repo_dir, tag):
            state[tag] = "nodocs"
            print(f"    skipped {tag} (no docs/mkdocs.yml at this tag)")
            continue
        try:
            run(["git", "-C", str(repo_dir), "checkout", "--quiet", "--force", tag])
            build(repo_dir, dest / tag, f"{base}/{tag}/", mkdocs)
            noindex(dest / tag)
            state[tag] = "built"
            print(f"    built {tag}")
        except Exception as e:
            failures.append(f"{name} {tag}: {detail(e)}")
            print(f"    FAILED {tag}")

    # The newest tag is built a second time as /latest/ rather than copied, so its
    # canonical points at itself. A copy would canonicalize to a noindex page.
    alias = next((t for t in reversed(wanted) if has_docs(repo_dir, t)), None)
    if alias is None:
        failures.append(f"{name}: no tag at or above {product.get('min_version')} has docs")
        alias = newest
    elif need_latest or args.force:
        try:
            run(["git", "-C", str(repo_dir), "checkout", "--quiet", "--force", alias])
            build(repo_dir, dest / "latest", f"{base}/latest/", mkdocs)
            state["__latest__"] = alias
            state["__latest_seen__"] = newest
            print(f"    built latest ({alias})")
        except Exception as e:
            failures.append(f"{name} latest: {detail(e)}")

    if want_dev and (need_dev or args.force):
        build_dev(product, repo_dir, dest, base, args, state, failures)

    published = [t for t in reversed(wanted) if (dest / t).exists()]
    if published:
        write_versions_json(dest, published, alias)
    write_redirect(dest / "index.html", "latest/", f"{base}/latest/")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source-root", help="build from local clones under this directory")
    p.add_argument("--only", action="append", help="limit to these product paths")
    p.add_argument("--force", action="store_true", help="rebuild versions already published")
    p.add_argument("--mkdocs", default="mkdocs", help="path to the mkdocs executable")
    args = p.parse_args()

    config = yaml.safe_load((ROOT / "products.yaml").read_text())
    args.site_url = config["site_url"].rstrip("/")
    products = config["products"]
    if args.only:
        products = [x for x in products if x["path"] in args.only]

    SITE.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() and not args.force else {}

    failures = []
    for product in products:
        print(f"{product['name']} ({product['repo']})")
        try:
            do_product(product, args, manifest, args.mkdocs, failures)
        except Exception as e:
            failures.append(f"{product['name']}: {e}")
            print(f"  FAILED: {e}")

    shutil.copytree(ROOT / "landing", SITE, dirs_exist_ok=True)
    (SITE / "CNAME").write_text(args.site_url.split("//")[1] + "\n", encoding="utf-8")
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    if failures:
        print("\nFailures (previously published copies left in place):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
