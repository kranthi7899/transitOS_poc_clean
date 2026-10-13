# Interactive repository explorer

The explorer uses OpenWiki's overview / architecture / source / learning pattern, with an original TransitOS implementation. OpenWiki was inspected at `dakjdakd/openwiki` (its English README is available); its analysis store is in-memory and its root licence is unspecified. No OpenWiki source, images, prompts or assets are copied.

## Run locally

```sh
make explorer-serve
```

Open **http://127.0.0.1:8111**. The build runs eight offline behavioral evals, records the existing operator-swap scenario through the actual integrated System, and writes the allowlisted static output to `site/`. It makes no model calls. Stop the server with Ctrl+C.

The site contains an overview, selectable architecture with source evidence, searchable source guide, learning path, focused eval report and a dispatcher replay. GitHub's README renderer cannot execute this JavaScript; the README links to the site / launch instructions rather than screenshots.

## Replay versus live console

The replay offers a recorded approval branch, rejection branch, delayed alert visibility, repair and separate return approval. Every displayed state was captured from fresh deterministic integration runs, including the direct repair path if alert visibility was not advanced first. Captures must have zero invariant violations. Approval clicks select recorded states; they never call the live backend, model or product adapters. The banner, controls and reasoning label identify this mode.

For actual execution, use `make console` or `make console-openai` (see [DEMO.md](DEMO.md)). The public replay contains no API key, runtime memory database, private local files or paid spend ledger. Its source guide is an explicit allowlist in `scripts/build_explorer.py`, not a repository-wide crawler. It shows source hashes and excerpts, and identifies the Git base commit for the working-tree build.

## Hosting

Live: **https://kranthi7899.github.io/transitOS_poc_clean/**. The first deployment succeeded on 2026-10-13 and was verified in Chrome.

GitHub Pages serves static HTML/CSS/JavaScript, not our Python backend ([GitHub documentation](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)). `site/` is ready to host as a static artifact. A manual Actions workflow packages the committed site and deploys it after Pages is configured for GitHub Actions. It does not publish on every push.

1. Decide the site's audience. A Pages site can be publicly readable even if the source repository is private; no repository visibility change is required by this implementation.
2. Repository **Settings → Pages → Source → GitHub Actions**.
3. **Actions → Interactive explorer → Run workflow** on `main`.
4. Use the deployment URL returned by GitHub; verify it before replacing the README's local launch link with a hosted link.

The repository destination is [transitOS_poc_clean](https://github.com/kranthi7899/transitOS_poc_clean). A successful source push is not proof that Pages is enabled; verify the deployment URL separately.

Rebuild with `make explorer` when the console or source guide changes. Runtime memory and source secrets are never exported. The deployment workflow has only contents-read, pages-write and id-token-write permissions, and publishes only `site/`.
