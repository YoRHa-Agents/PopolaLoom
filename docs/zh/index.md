---
layout: default
title: PopolaLoom
description: 架在 Cursor / Claude / Codex / Copilot 等本机 agent CLI 之上的元编排器。
lang: zh
translation_url: /index.html
---

<!-- updated: 2026-05-11 -->

<section class="hero">
  <h1>PopolaLoom</h1>
  <p class="tagline">织 agent 之机。</p>
  <p>PopolaLoom 是本机优先的 meta-orchestrator，架在 Cursor、Claude Code、Codex、Kimi、GitHub Copilot 等 agent CLI 之上，提供统一派发入口、持久任务总线和五通道 HITL。</p>
  <div class="cta-cluster">
    <a class="cta-button" href="QUICKSTART.html" aria-label="打开 PopolaLoom 五分钟快速开始">5 分钟上手</a>
    <a class="cta-button" href="https://github.com/YoRHa-Agents/PopolaLoom" aria-label="打开 PopolaLoom GitHub 仓库">GitHub</a>
    <a class="cta-button" href="USER_GUIDE.html" aria-label="阅读 PopolaLoom 用户指南">用户指南</a>
    <a class="cta-button" href="DEMO.html" aria-label="打开 PopolaLoom 演示">演示</a>
  </div>
</section>

<div class="kpi-strip" aria-label="PopolaLoom v0.9.x 关键指标">
  <div class="kpi-strip__item">5 channels HITL</div>
  <div class="kpi-strip__item">8 dim self-eval</div>
  <div class="kpi-strip__item">v0.9.x stable surface</div>
  <div class="kpi-strip__item">10 workflows</div>
</div>

## 选择你的路径

<div class="routing-grid">
  <a class="routing-card" href="QUICKSTART.html"><strong>新用户</strong><span>五分钟完成安装、init、dispatch 和 attach。</span></a>
  <a class="routing-card" href="demo-page.html#cloud-agent"><strong>Cloud dispatch</strong><span>用同一套 status / attach 表面派发 Cursor Cloud 任务。</span></a>
  <a class="routing-card" href="demo-page.html#self-hosted-worker"><strong>Self-hosted machine</strong><span>把本机注册成 worker，并安全 handoff。</span></a>
  <a class="routing-card" href="USER_GUIDE.html#mcp-集成"><strong>脚本化 / MCP</strong><span>使用 CLI JSON、handoff envelope 和 MCP 工具桥。</span></a>
</div>

<hr class="ornament">

## 一行安装

```bash
./install.sh install
popola init
popola auth cursor set --validate
popola popolad start
popola dispatch "echo hello popola" --cli=cursor
popola doctor
```

<hr class="ornament">

## 为什么是 PopolaLoom？

<div class="feature-grid">
  <div class="feature-card"><h3>统一派发入口</h3><p>`popola dispatch "..." --cli=cursor|claude|codex|kimi|copilot`，一条命令通吃本机 agent CLI。</p></div>
  <div class="feature-card"><h3>跨终端任务存活</h3><p>`popolad` UDS daemon + SQLite 任务池让任务在 shell 关闭、SSH 断线后仍可 attach。</p></div>
  <div class="feature-card"><h3>Cloud + self-hosted worker</h3><p>Cursor Cloud、self-hosted worker、`status`、`attach` 和 cancel 语义共用同一张表面。</p></div>
  <div class="feature-card"><h3>HITL 五通道</h3><p>Lark / IDE / CLI / MCP / Web 同步一个原子回答，先答者胜。</p></div>
  <div class="feature-card"><h3>Skill 自动发现</h3><p>`popola init` 把 canonical Skill 写进 Cursor / Claude / Codex / Copilot。</p></div>
  <div class="feature-card"><h3>8 维自评</h3><p>`popola eval run` 输出 dispatch isolation、HITL latency、attach correctness 等维度证据。</p></div>
</div>

<hr class="ornament">

## v0.9.x 时间线

<ol class="release-timeline">
  <li><strong>v0.8.5</strong> — Cursor Cloud dispatch 进入织机，并保留 task id、status、attach。</li>
  <li><strong>v0.9.0</strong> — GA 稳定边界锁定 CLI 动词、daemon RPC、JSON shape 和配置段名。</li>
  <li><strong>v0.9.7</strong> — `./install.sh install --with-credentials` 成为安全凭据安装路径。</li>
  <li><strong>v0.9.9</strong> — worker dispatch observability、pid drift 可见性和 init secret fallback 闭环。</li>
  <li><strong>v0.9.10</strong> — docs-site polish、扩展 demo 场景、用户偏好文档同步。</li>
</ol>

<hr class="ornament">

## 文档

- [快速开始](QUICKSTART.html)
- [用户指南](USER_GUIDE.html)
- [演示](DEMO.html)
- [演示页](demo-page.html)
- [设计思想](design-ideas.html)
- [已知限制](known-issues.html)
