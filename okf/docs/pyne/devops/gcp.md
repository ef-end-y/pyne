---
type: "Document"
title: "GCP & Cloud Run"
description: "Self-host the Pro API on Cloud Run with the checked-in Cloud Build pipeline — timeouts, memory, and app-layer API keys."
resource: "docs/pyne/devops/gcp.mdx"
tags: [devops, doc, docs, pyne]
status: stable
generated:
  by: process:axis-okf/1
  at: 2026-10-18T20:54:58Z
sources:
  - id: tree
    resource: "docs/pyne/devops/gcp.mdx"
    title: "docs/pyne/devops/gcp.mdx"
    author: process:git
okf_lock: generated
---

# Source

Repo path `docs/pyne/devops/gcp.mdx`.

# Outline

* Abstract
* Conceptual model
* Interface surface
  * Substitutions (cloudbuild.yaml)
  * Build steps
  * Cloud Run flags (as configured)
  * Build options
* Internals
* Invariants & edge cases
* Worked examples
  * Submit Cloud Build
  * Smoke health after deploy
* Failure modes
* See also

# Mentions

* [scripts/build](/code/scripts/build.md)
* [src/pynescript](/code/src/pynescript.md)
