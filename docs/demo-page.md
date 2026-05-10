---
layout: default
title: Demo Page
description: Visual walkthrough of PopolaLoom — pick your scenario and see the exact command flow.
lang: en
translation_url: /zh/demo-page.html
---

<!-- updated: 2026-05-11 -->

<section class="hero hero--small">
  <h1>PopolaLoom Demo Page</h1>
  <p class="tagline">Pick a scenario, copy the commands, then attach to the loom.</p>
  <p><a href="index.html">Back to docs</a></p>
</section>

## Scenario Picker

<div class="scenario-grid">
  <a class="scenario-card" href="#local-single-cli"><span class="scenario-card__badge">v0.1.0+</span><h3>Local single-CLI (Cursor)</h3><p>Run one local Cursor task while `popolad` keeps the event stream alive.</p><span class="scenario-card__link">Show flow</span></a>
  <a class="scenario-card" href="#cross-cli-handoff"><span class="scenario-card__badge">v0.7.0+</span><h3>Cross-CLI handoff</h3><p>Persist the prompt as a Markdown envelope and hand the next step to another CLI.</p><span class="scenario-card__link">Show flow</span></a>
  <a class="scenario-card" href="#hitl-pause"><span class="scenario-card__badge">v0.4.1+</span><h3>HITL pause + Lark approval</h3><p>Let five channels race toward one atomic answer.</p><span class="scenario-card__link">Show flow</span></a>
  <a class="scenario-card" href="#cloud-agent"><span class="scenario-card__badge">v0.8.5+</span><h3>Cloud Agent dispatch</h3><p>Use Cursor Cloud without giving up `status`, `attach`, or cancellation.</p><span class="scenario-card__link">Show flow</span></a>
  <a class="scenario-card" href="#self-hosted-worker"><span class="scenario-card__badge">v0.9.1+</span><h3>Self-hosted worker handoff</h3><p>Register this machine as a Cursor worker and choose who owns the run.</p><span class="scenario-card__link">Show flow</span></a>
  <a class="scenario-card" href="#cross-pr-relay"><span class="scenario-card__badge">v0.8.8+</span><h3>Cross-PR relay</h3><p>Turn one cloud task result into the next repo-aware cloud task.</p><span class="scenario-card__link">Show flow</span></a>
  <a class="scenario-card" href="#cli-preferences-wizard"><span class="scenario-card__badge">v0.9.10+</span><h3>CLI preferences wizard</h3><p>Use `popola init --interactive` Step 6 to record dispatch preferences.</p><span class="scenario-card__link">Show flow</span></a>
  <a class="scenario-card" href="#multi-cli-relay"><span class="scenario-card__badge">v0.9.10+</span><h3>Multi-CLI relay</h3><p>Cursor → Claude → Codex with `handoff list` and `dispatch --replay`.</p><span class="scenario-card__link">Show flow</span></a>
  <a class="scenario-card" href="#daemon-doctor-fix"><span class="scenario-card__badge">v0.9.10+</span><h3>Daemon doctor + fix</h3><p>Turn a red `popola doctor` check into all green checks.</p><span class="scenario-card__link">Show flow</span></a>
</div>

<section id="local-single-cli" class="demo-scenario">
  <h2>Local single-CLI (Cursor)</h2>
  <p>This is the smallest proof that PopolaLoom is a durable task bus, not a shell alias.</p>
  <details class="command-flow" open><summary>Command flow</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="hint-local-start">Start daemon before dispatching.</span>
<span class="cmd-line" aria-describedby="hint-local-start">popola popolad start</span>
<span class="cmd-line" aria-describedby="hint-local-start">popola dispatch "echo hello from popola" --cli=cursor</span>
<span class="cmd-line" aria-describedby="hint-local-start">popola attach &lt;task_id&gt; --follow</span></code></pre>
  </details>
  <div class="scenario-deliverables">
    <div class="deliverable event-sequence"><h3>Expected event sequence</h3><p>`task.dispatched` → `process.started` → `process.stdout` → `task.completed`.</p></div>
    <div class="deliverable pitfalls"><h3>Common pitfalls</h3><p>Refreshing the Cursor dashboard for a local subprocess; use `attach` instead.</p></div>
    <div class="deliverable verification-command"><h3>Verification command</h3><p>`popola status &lt;task_id&gt; --json | jq '.state'`</p></div>
    <div class="deliverable skill-workflow-link"><h3>Skill / Workflow link</h3><p><a href="USER_GUIDE.html#task-lifecycle">User Guide task lifecycle</a></p></div>
  </div>
</section>

<section id="cross-cli-handoff" class="demo-scenario">
  <h2>Cross-CLI handoff (Cursor → Claude)</h2>
  <p>Every dispatch writes a Markdown front-matter envelope before the adapter argv is built.</p>
  <details class="command-flow"><summary>Command flow</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="hint-handoff">Inspect the envelope before handing it to another CLI.</span>
<span class="cmd-line" aria-describedby="hint-handoff">popola dispatch "fix the NoneType bug in foo.py" --cli=cursor</span>
<span class="cmd-line" aria-describedby="hint-handoff">popola handoff list</span>
<span class="cmd-line" aria-describedby="hint-handoff">popola handoff show &lt;handoff_id&gt;</span>
<span class="cmd-line" aria-describedby="hint-handoff">popola dispatch "review the cursor fix and propose follow-up tests" --cli=claude --cwd "$(pwd)"</span></code></pre>
  </details>
  <div class="scenario-deliverables">
    <div class="deliverable event-sequence"><h3>Expected event sequence</h3><p>Cursor task completes, handoff envelope is listed, Claude task starts with a separate event log.</p></div>
    <div class="deliverable pitfalls"><h3>Common pitfalls</h3><p>Passing a stale handoff id after archiving; rerun `popola handoff list` first.</p></div>
    <div class="deliverable verification-command"><h3>Verification command</h3><p>`popola handoff show &lt;handoff_id&gt; --json`</p></div>
    <div class="deliverable skill-workflow-link"><h3>Skill / Workflow link</h3><p><a href="USER_GUIDE.html#hands-off-envelope">Hands-off envelope</a></p></div>
  </div>
</section>

<section id="hitl-pause" class="demo-scenario">
  <h2>HITL pause + Lark approval</h2>
  <p>When a LangGraph node calls `interrupt()`, the daemon publishes one pending HITL request to all configured channels.</p>
  <details class="command-flow"><summary>Command flow</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="hint-hitl">Answer exactly one pending HITL request.</span>
<span class="cmd-line" aria-describedby="hint-hitl">popola attach &lt;task_id&gt; --follow</span>
<span class="cmd-line" aria-describedby="hint-hitl">popola pending</span>
<span class="cmd-line" aria-describedby="hint-hitl">popola feedback hitl-abc12 yes --reason "verified backup taken"</span>
<span class="cmd-line" aria-describedby="hint-hitl">popola status &lt;task_id&gt; --json</span></code></pre>
  </details>
  <div class="scenario-deliverables">
    <div class="deliverable event-sequence"><h3>Expected event sequence</h3><p>`task.elicited` → one winning answer → `state.resumed` → terminal state.</p></div>
    <div class="deliverable pitfalls"><h3>Common pitfalls</h3><p>Late Lark and CLI answers are rejected after the first responder wins.</p></div>
    <div class="deliverable verification-command"><h3>Verification command</h3><p>`popola pending --json | jq 'length'`</p></div>
    <div class="deliverable skill-workflow-link"><h3>Skill / Workflow link</h3><p><a href="USER_GUIDE.html#hitl-workflow">HITL workflow</a></p></div>
  </div>
</section>

<section id="cloud-agent" class="demo-scenario">
  <h2>Cloud Agent dispatch</h2>
  <p>The cloud runtime swaps local `Popen` for Cursor Background Agent REST while preserving the same task id shape.</p>
  <details class="command-flow"><summary>Command flow</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="hint-cloud">Configure credentials before using cursor-cloud.</span>
<span class="cmd-line" aria-describedby="hint-cloud">./install.sh install --with-credentials</span>
<span class="cmd-line" aria-describedby="hint-cloud">popola auth cursor set --validate</span>
<span class="cmd-line" aria-describedby="hint-cloud">popola dispatch "Plan database migration scaffolding" --cli=cursor-cloud --cli-flag repo_url=https://github.com/acme/repo</span>
<span class="cmd-line" aria-describedby="hint-cloud">popola attach &lt;cloud_task_id&gt; --follow</span></code></pre>
  </details>
  <div class="scenario-deliverables">
    <div class="deliverable event-sequence"><h3>Expected event sequence</h3><p>`runtime=cloud` task appears, Cursor SSE streams first, poller remains source of terminal state.</p></div>
    <div class="deliverable pitfalls"><h3>Common pitfalls</h3><p>Missing GitHub integration or expired stream; PopolaLoom prints explicit cloud hints and falls back to polling.</p></div>
    <div class="deliverable verification-command"><h3>Verification command</h3><p>`popola list --json | jq '.[] | select(.runtime=="cloud")'`</p></div>
    <div class="deliverable skill-workflow-link"><h3>Skill / Workflow link</h3><p><a href="USER_GUIDE.html#cloud-agent-dispatch-v085">Cloud Agent dispatch</a></p></div>
  </div>
</section>

<section id="self-hosted-worker" class="demo-scenario">
  <h2>Self-hosted worker handoff</h2>
  <p>`popola cloud worker` wraps Cursor's worker CLI so this machine can appear in Cloud Agents UI.</p>
  <details class="command-flow"><summary>Command flow</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="hint-worker">Use debug before start to catch local worker prerequisites.</span>
<span class="cmd-line" aria-describedby="hint-worker">popola cloud worker debug --worker-dir "$(pwd)"</span>
<span class="cmd-line" aria-describedby="hint-worker">popola cloud worker start --worker-dir "$(pwd)"</span>
<span class="cmd-line" aria-describedby="hint-worker">popola cloud worker status --management-addr 127.0.0.1:39231 --json</span>
<span class="cmd-line" aria-describedby="hint-worker">popola cloud worker handoff --worker-dir "$(pwd)" --prompt "Run the migration smoke"</span></code></pre>
  </details>
  <div class="scenario-deliverables">
    <div class="deliverable event-sequence"><h3>Expected event sequence</h3><p>`debug` passes, worker starts or reuses singleton, `status` reports loopback health, handoff prints prompt + URL.</p></div>
    <div class="deliverable pitfalls"><h3>Common pitfalls</h3><p>Starting duplicates for the same directory; the default is singleton reuse unless `--allow-duplicate` is set.</p></div>
    <div class="deliverable verification-command"><h3>Verification command</h3><p>`popola cloud worker status --management-addr 127.0.0.1:39231 --json | jq '.ready'`</p></div>
    <div class="deliverable skill-workflow-link"><h3>Skill / Workflow link</h3><p><a href="USER_GUIDE.html#self-hosted-worker-handoff-popola-cloud-worker-v091">Self-hosted worker handoff</a></p></div>
  </div>
</section>

<section id="cross-pr-relay" class="demo-scenario">
  <h2>Cross-PR relay</h2>
  <p>Relay turns the result of one cloud task into the prompt for another cloud task with allowlist and audit gates.</p>
  <details class="command-flow"><summary>Command flow</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="hint-relay">Dry-run first so the policy decision is visible.</span>
<span class="cmd-line" aria-describedby="hint-relay">popola relay &lt;source_cloud_task_id&gt; --dry-run</span>
<span class="cmd-line" aria-describedby="hint-relay">popola relay &lt;source_cloud_task_id&gt; --target-repo https://github.com/acme/other-repo --confirm-allowlist</span>
<span class="cmd-line" aria-describedby="hint-relay">popola attach &lt;relay_task_id&gt; --follow</span></code></pre>
  </details>
  <div class="scenario-deliverables">
    <div class="deliverable event-sequence"><h3>Expected event sequence</h3><p>Policy dry-run, allowlist decision, new `cursor-cloud` task, attachable relay event stream.</p></div>
    <div class="deliverable pitfalls"><h3>Common pitfalls</h3><p>Empty repo allowlist blocks by default; `--confirm-allowlist` records an override.</p></div>
    <div class="deliverable verification-command"><h3>Verification command</h3><p>`popola relay &lt;source_cloud_task_id&gt; --dry-run --json | jq '.outcome'`</p></div>
    <div class="deliverable skill-workflow-link"><h3>Skill / Workflow link</h3><p><a href="USER_GUIDE.html#cross-pr-relay--popola-relay-v088">Cross-PR relay</a></p></div>
  </div>
</section>

<section id="cli-preferences-wizard" class="demo-scenario">
  <h2>CLI preferences wizard</h2>
  <figure class="scenario-figure"><img src="assets/img/demos/cli-preferences-wizard.svg" alt="CLI preferences wizard placeholder"></figure>
  <p>Step 6 of `popola init --interactive` records repeatable dispatch preferences so future commands can opt in explicitly.</p>
  <details class="command-flow"><summary>Command flow</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="hint-prefs">Answer Step 6 prompts with routing defaults, not secrets.</span>
<span class="cmd-line" aria-describedby="hint-prefs">popola init --interactive</span>
<span class="cmd-line" aria-describedby="hint-prefs"># Step 6/6: User preferences</span>
<span class="cmd-line" aria-describedby="hint-prefs"># 1. Select default CLI: cursor</span>
<span class="cmd-line" aria-describedby="hint-prefs"># 2. Select default workspace: $(pwd)</span>
<span class="cmd-line" aria-describedby="hint-prefs"># 3. Prefer streaming attach? yes</span>
<span class="cmd-line" aria-describedby="hint-prefs"># 4. Confirm before cloud dispatch? yes</span>
<span class="cmd-line" aria-describedby="hint-prefs"># 5. Save profile name: daily-driver</span>
<span class="cmd-line" aria-describedby="hint-prefs">popola dispatch "summarize the repo" --use-preferences</span></code></pre>
  </details>
  <div class="scenario-deliverables">
    <div class="deliverable event-sequence"><h3>Expected event sequence</h3><p>Wizard writes `[user_preferences]`, profile is selected, dispatch expands defaults, task emits normal local events.</p></div>
    <div class="deliverable pitfalls"><h3>Common pitfalls</h3><p>Do not store API keys in preferences; keep credentials in keyring or `CURSOR_API_KEY`.</p></div>
    <div class="deliverable verification-command"><h3>Verification command</h3><p>`popola doctor --json | jq '.user_preferences'`</p></div>
    <div class="deliverable skill-workflow-link"><h3>Skill / Workflow link</h3><p><a href="USER_GUIDE.html#user-preferences-v0910">User preferences</a></p></div>
  </div>
</section>

<section id="multi-cli-relay" class="demo-scenario">
  <h2>Multi-CLI relay (Cursor → Claude → Codex)</h2>
  <figure class="scenario-figure"><img src="assets/img/demos/multi-cli-relay.svg" alt="Multi-CLI relay placeholder"></figure>
  <p>The envelope chain lets each CLI own a clean subprocess while the audit trail stays visible.</p>
  <details class="command-flow"><summary>Command flow</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="hint-multi-relay">Use replay ids to avoid copy-pasting long prompts.</span>
<span class="cmd-line" aria-describedby="hint-multi-relay">popola dispatch "design the cache migration" --cli=cursor --wait</span>
<span class="cmd-line" aria-describedby="hint-multi-relay">popola handoff list</span>
<span class="cmd-line" aria-describedby="hint-multi-relay">popola dispatch --replay &lt;cursor_handoff_id&gt; --cli=claude --wait</span>
<span class="cmd-line" aria-describedby="hint-multi-relay">popola handoff list</span>
<span class="cmd-line" aria-describedby="hint-multi-relay">popola dispatch --replay &lt;claude_handoff_id&gt; --cli=codex --cli-flag sandbox=read-only</span></code></pre>
  </details>
  <div class="scenario-deliverables">
    <div class="deliverable event-sequence"><h3>Expected event sequence</h3><p>Cursor creates design envelope, Claude replays it into implementation review, Codex replays Claude's envelope in read-only verification.</p></div>
    <div class="deliverable pitfalls"><h3>Common pitfalls</h3><p>Replaying the wrong envelope causes role drift; inspect `target_cli` and summary before dispatch.</p></div>
    <div class="deliverable verification-command"><h3>Verification command</h3><p>`popola handoff list --json | jq '.[0].handoff_id'`</p></div>
    <div class="deliverable skill-workflow-link"><h3>Skill / Workflow link</h3><p><a href="USER_GUIDE.html#hands-off-envelope">Workflow 2 / hands-off envelope</a></p></div>
  </div>
</section>

<section id="daemon-doctor-fix" class="demo-scenario">
  <h2>Daemon doctor + fix</h2>
  <figure class="scenario-figure"><img src="assets/img/demos/daemon-doctor-fix.svg" alt="Daemon doctor fix placeholder"></figure>
  <p>Use doctor as a short red-to-green path before demos or CI smoke runs.</p>
  <details class="command-flow"><summary>Command flow</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="hint-doctor">Fix the failing subsystem shown by doctor, then rerun.</span>
<span class="cmd-line" aria-describedby="hint-doctor">popola doctor</span>
<span class="cmd-line" aria-describedby="hint-doctor"># ✗ popolad: socket missing</span>
<span class="cmd-line" aria-describedby="hint-doctor">popola popolad start</span>
<span class="cmd-line" aria-describedby="hint-doctor">popola doctor --json</span>
<span class="cmd-line" aria-describedby="hint-doctor"># ✓ all checks</span></code></pre>
  </details>
  <div class="scenario-deliverables">
    <div class="deliverable event-sequence"><h3>Expected event sequence</h3><p>Doctor reports one red subsystem, the fix command starts the daemon, rerun returns all checks green.</p></div>
    <div class="deliverable pitfalls"><h3>Common pitfalls</h3><p>A stale socket can mimic a daemon failure; stop first if start reports an existing daemon.</p></div>
    <div class="deliverable verification-command"><h3>Verification command</h3><p>`popola doctor --json | jq '.overall'`</p></div>
    <div class="deliverable skill-workflow-link"><h3>Skill / Workflow link</h3><p><a href="USER_GUIDE.html#health--diagnostics">Health + diagnostics</a></p></div>
  </div>
</section>

Generated 2026-05-11 against PopolaLoom v1.0.0-pre.1. For walkthroughs with full output, see [DEMO.md](DEMO.html).
