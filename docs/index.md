---
layout: default
title: PopolaLoom
description: Meta-orchestrator over local agent CLIs (Cursor / Claude / Codex / Copilot)
---

<!-- updated: 2026-05-10 -->

<section class="hero">
  <h1 data-i18n="hero.title">PopolaLoom</h1>
  <p class="tagline" data-i18n="hero.tagline">A loom that weaves agents.</p>
  <p data-i18n="hero.lead">Local-first meta-orchestrator that sits on top of every agent CLI on your machine — Cursor, Claude Code, Codex, Kimi, GitHub Copilot — and gives them a single dispatch surface, a persistent task bus, and a unified HITL channel.</p>
  <div class="cta-cluster">
    <a class="cta-button" href="QUICKSTART.html" data-i18n="hero.cta_start">5-minute Quickstart</a>
    <a class="cta-button" href="https://github.com/YoRHa-Agents/PopolaLoom" data-i18n="hero.cta_github">GitHub</a>
    <a class="cta-button" href="USER_GUIDE.html" data-i18n="hero.cta_guide">User Guide</a>
    <a class="cta-button" href="DEMO.html" data-i18n="hero.cta_demo">Demo</a>
  </div>
</section>

<hr class="ornament">

<h2 data-i18n="install.heading">Install in one line</h2>

```bash
./install.sh install                 # canonical v0.9.7 path
popola init                          # auto-detect Cursor / Claude / Codex / Copilot
popola auth cursor set --validate    # optional: Cursor Cloud API key in OS keyring
popola popolad start                 # boot the UDS daemon
popola dispatch "echo hello popola" --cli=cursor
popola doctor                        # 4-subsystem health check
```

<hr class="ornament">

<h2 data-i18n="features.heading">Why PopolaLoom?</h2>

<div class="feature-grid">
  <div class="feature-card">
    <h3 data-i18n="feature.dispatch.title">Single dispatch surface</h3>
    <p data-i18n="feature.dispatch.body">popola dispatch "..." --cli=cursor|claude|codex|kimi|copilot — one command shape across every agent CLI on your machine.</p>
  </div>
  <div class="feature-card">
    <h3 data-i18n="feature.survival.title">Cross-terminal task survival</h3>
    <p data-i18n="feature.survival.body">popolad UDS daemon with persistent SQLite task pool; tasks outlive shell close, SSH disconnect, even reboots.</p>
  </div>
  <div class="feature-card">
    <h3 data-i18n="feature.cloud.title">Cloud + Self-hosted worker (v0.8.5–v0.9.3)</h3>
    <p data-i18n="feature.cloud.body">Dispatch to Cursor Cloud, stream cloud runs, or register this machine with popola cloud worker while keeping popola status / attach semantics consistent.</p>
  </div>
  <div class="feature-card">
    <h3 data-i18n="feature.hitl.title">HITL across 5 channels</h3>
    <p data-i18n="feature.hitl.body">Lark / IDE / CLI / MCP / Web — first responder wins via atomic cross-channel sync. LangGraph interrupt() broadcasts to all five.</p>
  </div>
  <div class="feature-card">
    <h3 data-i18n="feature.skill.title">Skill-based auto-discovery</h3>
    <p data-i18n="feature.skill.body">popola init writes SKILL.md into Cursor / Claude / Codex / Copilot; host agents auto-load it. install-popola Skill (v0.7.0+) walks fresh installs.</p>
  </div>
  <div class="feature-card">
    <h3 data-i18n="feature.eval.title">8-dim PopolaLoom-nines self-eval</h3>
    <p data-i18n="feature.eval.body">popola eval run produces a composite score with per-dimension evidence pipelines — dispatch_isolation, hitl_latency, attach_correctness, cross_cli_handoff, and four more.</p>
  </div>
  <div class="feature-card">
    <h3 data-i18n="feature.credentials.title">Secure credential storage (v0.9.2+)</h3>
    <p data-i18n="feature.credentials.body">popola auth cursor set --validate stores the Cursor API key in the OS keyring; v0.9.7 adds ./install.sh install --with-credentials for the optional backend.</p>
  </div>
</div>

<hr class="ornament">

<h2 data-i18n="design_ideas.heading">Core design ideas</h2>

<ul>
  <li data-i18n="design_ideas.loom">Loom, not router — N agent strands become one audited tapestry through dispatch, handoff files, and replay-by-id.</li>
  <li data-i18n="design_ideas.sidecar">Sidecar daemon — popolad owns process lifetime, SQLite state, event logs, cloud polling, and HITL so terminals can disappear safely.</li>
  <li data-i18n="design_ideas.boundary">Automation-grade boundary — v0.9.0+ locks CLI verbs, daemon RPC routes, --json schemas, and config section names while marking experiments explicitly.</li>
</ul>

<p><a href="design-ideas.html" data-i18n="design_ideas.link">Read the full design philosophy →</a></p>

<hr class="ornament">

<h2 data-i18n="design.heading">Design in one picture</h2>

<div class="feature-grid">
  <div class="feature-card">
    <h3 data-i18n="design.sidecar.title">Sidecar, not another IDE</h3>
    <p data-i18n="design.sidecar.body">The `popolad` daemon owns process lifetime, task state, and event logs while each agent CLI stays isolated in its own subprocess.</p>
  </div>
  <div class="feature-card">
    <h3 data-i18n="design.envelope.title">File-backed handoff</h3>
    <p data-i18n="design.envelope.body">Long prompts become audited Markdown envelopes with slug-hash ids, avoiding argv limits and making replay deterministic.</p>
  </div>
  <div class="feature-card">
    <h3 data-i18n="design.hitl.title">Human-in-the-loop fanout</h3>
    <p data-i18n="design.hitl.body">Interrupts fan out to Lark, IDE, CLI, MCP, and web surfaces; one atomic answer resumes the task and late replies back off.</p>
  </div>
</div>

<hr class="ornament">

<h2 data-i18n="docs.heading">Documentation</h2>

<ul>
  <li><a href="QUICKSTART.html" data-i18n="docs.quickstart">Quickstart — 5-minute onboarding (install → first task)</a></li>
  <li><a href="USER_GUIDE.html" data-i18n="docs.user_guide">User Guide — full reference (CLI verbs, MCP, HITL, Lark, hands-off envelope)</a></li>
  <li><a href="DEMO.html" data-i18n="docs.demo">Demo — walkthroughs, example outputs, self-evolution journey</a></li>
  <li><a href="demo-page.html" data-i18n="docs.demo_page">Demo Page — scenario cards and terminal-recording command flows</a></li>
  <li><a href="design-ideas.html" data-i18n="docs.design_ideas">Design Ideas — the seven architectural choices behind the loom</a></li>
  <li><a href="https://github.com/YoRHa-Agents/PopolaLoom/blob/main/RELEASE_NOTES.md" data-i18n="docs.release_notes">Release Notes (v0.8.0+ floats here)</a></li>
  <li><a href="https://github.com/YoRHa-Agents/PopolaLoom/blob/main/CHANGELOG.md" data-i18n="docs.changelog">CHANGELOG — full v0.0.1 → v0.8.x history</a></li>
</ul>

<hr class="ornament">

<h2 data-i18n="status.heading">Project status</h2>

<p data-i18n="status.lead">v0.9.7 carries forward the v0.9.0 GA stability boundary while refreshing the public docs for cloud workers, secure key storage, and Cloud HITL gamma. Cursor Cloud dispatch, self-hosted worker handoff, multi-run attach, cross-PR relay, and file-backed handoff now share one documented surface. The safe install path is ./install.sh install, with ./install.sh install --with-credentials for OS-keyring-backed Cursor API keys.</p>
