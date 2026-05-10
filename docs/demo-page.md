---
layout: default
title: Demo Page
description: Visual walkthrough of PopolaLoom — pick your scenario and see the exact command flow.
lang: en
translation_url: /zh/demo-page.html
---

<!-- updated: 2026-05-10 -->

<section class="hero hero--small">
  <h1>PopolaLoom Demo Page</h1>
  <p class="tagline">Pick a scenario, copy the commands, then attach to the loom.</p>
  <p><a href="index.html">Back to docs</a></p>
</section>

## Scenario Picker

<div class="scenario-grid">
  <a class="scenario-card" href="#local-single-cli">
    <span class="scenario-card__badge">v0.1.0+</span>
    <h3>Local single-CLI (Cursor)</h3>
    <p>Run one local Cursor task while `popolad` keeps the event stream alive after the terminal closes.</p>
    <span class="scenario-card__link">Show flow</span>
  </a>
  <a class="scenario-card" href="#cross-cli-handoff">
    <span class="scenario-card__badge">v0.7.0+</span>
    <h3>Cross-CLI handoff</h3>
    <p>Persist the prompt as a Markdown envelope, inspect it, and hand the next step to another CLI.</p>
    <span class="scenario-card__link">Show flow</span>
  </a>
  <a class="scenario-card" href="#hitl-pause">
    <span class="scenario-card__badge">v0.4.1+</span>
    <h3>HITL pause + Lark approval</h3>
    <p>Let a task pause for approval while Lark, IDE, CLI, MCP, and Web race toward one answer.</p>
    <span class="scenario-card__link">Show flow</span>
  </a>
  <a class="scenario-card" href="#cloud-agent">
    <span class="scenario-card__badge">v0.8.5+</span>
    <h3>Cloud Agent dispatch</h3>
    <p>Use the Cursor Cloud runtime without giving up `popola status`, `attach`, or cancellation.</p>
    <span class="scenario-card__link">Show flow</span>
  </a>
  <a class="scenario-card" href="#self-hosted-worker">
    <span class="scenario-card__badge">v0.9.1+</span>
    <h3>Self-hosted worker handoff</h3>
    <p>Register this machine as a Cursor worker and decide whether the dashboard or PopolaLoom owns the run.</p>
    <span class="scenario-card__link">Show flow</span>
  </a>
  <a class="scenario-card" href="#cross-pr-relay">
    <span class="scenario-card__badge">v0.8.8+</span>
    <h3>Cross-PR relay</h3>
    <p>Turn one cloud task's result into the input for the next repo-aware cloud task.</p>
    <span class="scenario-card__link">Show flow</span>
  </a>
</div>

<section id="local-single-cli">
  <h2>Local single-CLI (Cursor)</h2>
  <p>This is the smallest proof that PopolaLoom is a durable task bus, not a shell alias. The Cursor subprocess runs under `popolad`, and `attach` replays the same NDJSON event stream from any terminal.</p>
  <pre class="terminal-block terminal-block--active"><code># install and register the Skill
./install.sh install
popola init cursor --global

# start the sidecar and dispatch one local task
popola popolad start
popola dispatch "echo hello from popola" --cli=cursor

# copy the returned task id, then follow the stream
popola attach &lt;task_id&gt; --follow</code></pre>
  <p>Expect `process.stdout`, `process.stderr`, `state.*`, and a terminal `task.completed` event; `Ctrl-C` exits attach only.</p>
  <p><a href="USER_GUIDE.html#task-lifecycle">Read deep-dive</a></p>
</section>

<section id="cross-cli-handoff">
  <h2>Cross-CLI handoff (Cursor → Claude)</h2>
  <p>Every dispatch writes a Markdown front-matter envelope before the adapter argv is built. That file is the audit receipt you can show, archive, replay, or use as the handoff artifact for a second CLI.</p>
  <pre class="terminal-block terminal-block--active"><code># create the first task and its handoff envelope
popola dispatch "fix the NoneType bug in foo.py" --cli=cursor
popola handoff list
popola handoff show &lt;handoff_id&gt;

# hand the reviewed context to another local CLI
popola dispatch "review the cursor fix and propose follow-up tests" --cli=claude --cwd "$(pwd)"
popola attach &lt;claude_task_id&gt; --follow</code></pre>
  <p>Expect the envelope id to be slug-hash stable; the second task is a separate subprocess with its own event log.</p>
  <p><a href="USER_GUIDE.html#hands-off-envelope">Read deep-dive</a></p>
</section>

<section id="hitl-pause">
  <h2>HITL pause + Lark approval</h2>
  <p>When a LangGraph node calls `interrupt()`, the daemon publishes one pending HITL request to all configured channels. Lark may be absent; the local CLI and NDJSON state still carry the request.</p>
  <pre class="terminal-block terminal-block--active"><code># terminal A: watch the task pause
popola attach &lt;task_id&gt; --follow

# terminal B: inspect and answer from the CLI channel
popola pending
popola feedback hitl-abc12 yes --reason "verified backup taken"

# terminal A resumes after state.resumed
popola status &lt;task_id&gt; --json</code></pre>
  <p>Expect `task.elicited` followed by exactly one winning answer and a `state.resumed` event; late channels see the already-answered status.</p>
  <p><a href="USER_GUIDE.html#hitl-workflow">Read deep-dive</a></p>
</section>

<section id="cloud-agent">
  <h2>Cloud Agent dispatch</h2>
  <p>The cloud runtime swaps local `Popen` for Cursor's Background Agent REST while preserving the same task id, status, attach, and cancel shape. Configure credentials first, then route the dispatch through `--cli=cursor-cloud`.</p>
  <pre class="terminal-block terminal-block--active"><code># one-time credential setup
./install.sh install --with-credentials
popola auth cursor set --validate

# create a Cursor Cloud task through popolad
popola popolad start
popola dispatch "Plan database migration scaffolding" \
  --cli=cursor-cloud \
  --cli-flag repo_url=https://github.com/acme/repo

popola attach &lt;cloud_task_id&gt; --follow</code></pre>
  <p>Expect `popola list` to show `runtime=cloud`; attach streams Cursor SSE first and falls back to polling when a stream expires.</p>
  <p><a href="USER_GUIDE.html#cloud-agent-dispatch-v085">Read deep-dive</a></p>
</section>

<section id="self-hosted-worker">
  <h2>Self-hosted worker handoff</h2>
  <p>`popola cloud worker` wraps Cursor's worker CLI so this machine can appear in Cloud Agents UI. `handoff` is side-effect-free; `dispatch` creates a normal PopolaLoom-tracked cloud task routed to the workspace worker.</p>
  <pre class="terminal-block terminal-block--active"><code># terminal A: preflight and start or reuse the workspace worker
popola cloud worker debug --worker-dir "$(pwd)"
popola cloud worker start --worker-dir "$(pwd)"

# terminal B: inspect, hand off, or dispatch through popolad
popola cloud worker status --management-addr 127.0.0.1:39231 --json
popola cloud worker handoff --worker-dir "$(pwd)" --prompt "Run the migration smoke"
popola cloud worker dispatch "Run the migration smoke" --worker-dir "$(pwd)" --print-only</code></pre>
  <p>Expect duplicate `start` calls for the same worker dir to reuse the existing worker unless `--allow-duplicate` is passed.</p>
  <p><a href="USER_GUIDE.html#self-hosted-worker-handoff-popola-cloud-worker-v091">Read deep-dive</a></p>
</section>

<section id="cross-pr-relay">
  <h2>Cross-PR relay</h2>
  <p>Relay turns the result of one cloud task into the prompt for another cloud task. The default is automation-first, guarded by allowlist, audit log, idempotency, and secret-scan mitigations.</p>
  <pre class="terminal-block terminal-block--active"><code># preview the relay payload first
popola relay &lt;source_cloud_task_id&gt; --dry-run

# dispatch to an allowed target repo, recording the policy decision
popola relay &lt;source_cloud_task_id&gt; \
  --target-repo https://github.com/acme/other-repo \
  --confirm-allowlist

popola attach &lt;relay_task_id&gt; --follow</code></pre>
  <p>Expect either a policy-denied exit or a new `cursor-cloud` task that is observable like any hand-typed cloud dispatch.</p>
  <p><a href="USER_GUIDE.html#cross-pr-relay--popola-relay-v088">Read deep-dive</a></p>
</section>

Generated 2026-05-10 against PopolaLoom v0.9.7. For walkthroughs with full output, see [DEMO.md](DEMO.html).
