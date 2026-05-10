---
layout: default
title: 演示页
description: PopolaLoom 视觉化 walkthrough：选择场景，查看精确命令流。
lang: zh
translation_url: /demo-page.html
---

<!-- updated: 2026-05-11 -->

<section class="hero hero--small">
  <h1>PopolaLoom 演示页</h1>
  <p class="tagline">选一个场景，照着命令跑，再 attach 到织机的事件流。</p>
  <p><a href="index.html">返回文档首页</a></p>
</section>

## 场景选择

<div class="scenario-grid">
  <a class="scenario-card" href="#local-single-cli"><span class="scenario-card__badge">v0.1.0+</span><h3>本地单 CLI（Cursor）</h3><p>让 `popolad` 保留本地 Cursor 任务事件流。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#cross-cli-handoff"><span class="scenario-card__badge">v0.7.0+</span><h3>跨 CLI handoff</h3><p>把 prompt 写成 Markdown 信封，再交给另一个 CLI。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#hitl-pause"><span class="scenario-card__badge">v0.4.1+</span><h3>HITL 暂停 + Lark 审批</h3><p>五个通道抢答同一个原子回答。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#cloud-agent"><span class="scenario-card__badge">v0.8.5+</span><h3>Cloud Agent 派发</h3><p>使用 Cursor Cloud，同时保留 `status`、`attach`、cancel。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#self-hosted-worker"><span class="scenario-card__badge">v0.9.1+</span><h3>Self-hosted worker handoff</h3><p>把本机注册成 Cursor worker，再选择由谁创建 run。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#cross-pr-relay"><span class="scenario-card__badge">v0.8.8+</span><h3>跨 PR relay</h3><p>把一个 cloud task 的结果变成下一个 repo-aware cloud task。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#cli-preferences-wizard"><span class="scenario-card__badge">v0.9.10+</span><h3>CLI preferences wizard</h3><p>`popola init --interactive` Step 6 记录派发偏好。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#multi-cli-relay"><span class="scenario-card__badge">v0.9.10+</span><h3>多 CLI relay</h3><p>Cursor → Claude → Codex，使用 `handoff list` 和 `dispatch --replay`。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#daemon-doctor-fix"><span class="scenario-card__badge">v0.9.10+</span><h3>Daemon doctor + fix</h3><p>把红色 `popola doctor` 检查修到全绿。</p><span class="scenario-card__link">查看流程</span></a>
</div>

<section id="local-single-cli" class="demo-scenario">
  <h2>本地单 CLI（Cursor）</h2>
  <p>最小路径证明 PopolaLoom 是持久任务总线，而不是 shell alias。</p>
  <details class="command-flow" open><summary>命令流</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="zh-hint-local">派发前先启动 daemon。</span>
<span class="cmd-line" aria-describedby="zh-hint-local">popola popolad start</span>
<span class="cmd-line" aria-describedby="zh-hint-local">popola dispatch "echo hello from popola" --cli=cursor</span>
<span class="cmd-line" aria-describedby="zh-hint-local">popola attach &lt;task_id&gt; --follow</span></code></pre>
  </details>
  <div class="scenario-deliverables">
    <div class="deliverable event-sequence"><h3>预期事件序列</h3><p>`task.dispatched` → `process.started` → `process.stdout` → `task.completed`。</p></div>
    <div class="deliverable pitfalls"><h3>常见坑</h3><p>本地 subprocess 不会出现在 Cursor dashboard；请用 `attach`。</p></div>
    <div class="deliverable verification-command"><h3>验证命令</h3><p>`popola status &lt;task_id&gt; --json | jq '.state'`</p></div>
    <div class="deliverable skill-workflow-link"><h3>Skill / Workflow 链接</h3><p><a href="USER_GUIDE.html#cli-速查">CLI 速查</a></p></div>
  </div>
</section>

<section id="cross-cli-handoff" class="demo-scenario">
  <h2>跨 CLI handoff（Cursor → Claude）</h2>
  <p>每次 dispatch 都先写 Markdown front-matter 信封，再构造 adapter argv。</p>
  <details class="command-flow"><summary>命令流</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="zh-hint-handoff">交给另一个 CLI 前先检查 envelope。</span>
<span class="cmd-line" aria-describedby="zh-hint-handoff">popola dispatch "fix the NoneType bug in foo.py" --cli=cursor</span>
<span class="cmd-line" aria-describedby="zh-hint-handoff">popola handoff list</span>
<span class="cmd-line" aria-describedby="zh-hint-handoff">popola handoff show &lt;handoff_id&gt;</span>
<span class="cmd-line" aria-describedby="zh-hint-handoff">popola dispatch "review the cursor fix and propose follow-up tests" --cli=claude --cwd "$(pwd)"</span></code></pre>
  </details>
  <div class="scenario-deliverables"><div class="deliverable event-sequence"><h3>预期事件序列</h3><p>Cursor 任务完成，信封可列出，Claude 任务用独立事件日志启动。</p></div><div class="deliverable pitfalls"><h3>常见坑</h3><p>archive 后再用旧 handoff id；先重新 `popola handoff list`。</p></div><div class="deliverable verification-command"><h3>验证命令</h3><p>`popola handoff show &lt;handoff_id&gt; --json`</p></div><div class="deliverable skill-workflow-link"><h3>Skill / Workflow 链接</h3><p><a href="USER_GUIDE.html#hands-off-envelope">Hands-off envelope</a></p></div></div>
</section>

<section id="hitl-pause" class="demo-scenario">
  <h2>HITL 暂停 + Lark 审批</h2>
  <p>LangGraph 节点调用 `interrupt()` 后，daemon 会把一个 HITL 请求发到所有可用通道。</p>
  <details class="command-flow"><summary>命令流</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="zh-hint-hitl">只回答一个 pending HITL 请求。</span>
<span class="cmd-line" aria-describedby="zh-hint-hitl">popola attach &lt;task_id&gt; --follow</span>
<span class="cmd-line" aria-describedby="zh-hint-hitl">popola pending</span>
<span class="cmd-line" aria-describedby="zh-hint-hitl">popola feedback hitl-abc12 yes --reason "verified backup taken"</span>
<span class="cmd-line" aria-describedby="zh-hint-hitl">popola status &lt;task_id&gt; --json</span></code></pre>
  </details>
  <div class="scenario-deliverables"><div class="deliverable event-sequence"><h3>预期事件序列</h3><p>`task.elicited` → 一个获胜回答 → `state.resumed` → 终态。</p></div><div class="deliverable pitfalls"><h3>常见坑</h3><p>第一个回答胜出后，迟到的 Lark / CLI 回答会被拒绝。</p></div><div class="deliverable verification-command"><h3>验证命令</h3><p>`popola pending --json | jq 'length'`</p></div><div class="deliverable skill-workflow-link"><h3>Skill / Workflow 链接</h3><p><a href="USER_GUIDE.html#hitl-工作流">HITL 工作流</a></p></div></div>
</section>

<section id="cloud-agent" class="demo-scenario">
  <h2>Cloud Agent 派发</h2>
  <p>Cloud runtime 把本地 `Popen` 换成 Cursor Background Agent REST，但保留同样的 task id 形状。</p>
  <details class="command-flow"><summary>命令流</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="zh-hint-cloud">先配置凭据，再使用 cursor-cloud。</span>
<span class="cmd-line" aria-describedby="zh-hint-cloud">./install.sh install --with-credentials</span>
<span class="cmd-line" aria-describedby="zh-hint-cloud">popola auth cursor set --validate</span>
<span class="cmd-line" aria-describedby="zh-hint-cloud">popola dispatch "Plan database migration scaffolding" --cli=cursor-cloud --cli-flag repo_url=https://github.com/acme/repo</span>
<span class="cmd-line" aria-describedby="zh-hint-cloud">popola attach &lt;cloud_task_id&gt; --follow</span></code></pre>
  </details>
  <div class="scenario-deliverables"><div class="deliverable event-sequence"><h3>预期事件序列</h3><p>`runtime=cloud` 任务出现，Cursor SSE 优先流入，poller 仍负责终态。</p></div><div class="deliverable pitfalls"><h3>常见坑</h3><p>GitHub integration 缺失或 stream 过期；PopolaLoom 会显式提示并 fallback 到 polling。</p></div><div class="deliverable verification-command"><h3>验证命令</h3><p>`popola list --json | jq '.[] | select(.runtime=="cloud")'`</p></div><div class="deliverable skill-workflow-link"><h3>Skill / Workflow 链接</h3><p><a href="USER_GUIDE.html#credentials-与安全存储v092">Cloud 凭据</a></p></div></div>
</section>

<section id="self-hosted-worker" class="demo-scenario">
  <h2>Self-hosted worker handoff</h2>
  <p>`popola cloud worker` 包装 Cursor worker CLI，让这台机器出现在 Cloud Agents UI。</p>
  <details class="command-flow"><summary>命令流</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="zh-hint-worker">start 前先用 debug 捕获本地前置条件。</span>
<span class="cmd-line" aria-describedby="zh-hint-worker">popola cloud worker debug --worker-dir "$(pwd)"</span>
<span class="cmd-line" aria-describedby="zh-hint-worker">popola cloud worker start --worker-dir "$(pwd)"</span>
<span class="cmd-line" aria-describedby="zh-hint-worker">popola cloud worker status --management-addr 127.0.0.1:39231 --json</span>
<span class="cmd-line" aria-describedby="zh-hint-worker">popola cloud worker handoff --worker-dir "$(pwd)" --prompt "Run the migration smoke"</span></code></pre>
  </details>
  <div class="scenario-deliverables"><div class="deliverable event-sequence"><h3>预期事件序列</h3><p>`debug` 通过，worker 启动或复用 singleton，`status` 报 loopback health，handoff 输出 prompt + URL。</p></div><div class="deliverable pitfalls"><h3>常见坑</h3><p>同目录重复 start；默认复用 singleton，除非传 `--allow-duplicate`。</p></div><div class="deliverable verification-command"><h3>验证命令</h3><p>`popola cloud worker status --management-addr 127.0.0.1:39231 --json | jq '.ready'`</p></div><div class="deliverable skill-workflow-link"><h3>Skill / Workflow 链接</h3><p><a href="USER_GUIDE.html#self-hosted-worker-handoffv091">Self-hosted worker</a></p></div></div>
</section>

<section id="cross-pr-relay" class="demo-scenario">
  <h2>跨 PR relay</h2>
  <p>Relay 通过 allowlist 和 audit gate，把一个 cloud task 的结果变成另一个 cloud task 的 prompt。</p>
  <details class="command-flow"><summary>命令流</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="zh-hint-relay">先 dry-run，让 policy 决策可见。</span>
<span class="cmd-line" aria-describedby="zh-hint-relay">popola relay &lt;source_cloud_task_id&gt; --dry-run</span>
<span class="cmd-line" aria-describedby="zh-hint-relay">popola relay &lt;source_cloud_task_id&gt; --target-repo https://github.com/acme/other-repo --confirm-allowlist</span>
<span class="cmd-line" aria-describedby="zh-hint-relay">popola attach &lt;relay_task_id&gt; --follow</span></code></pre>
  </details>
  <div class="scenario-deliverables"><div class="deliverable event-sequence"><h3>预期事件序列</h3><p>policy dry-run、allowlist 决策、新 `cursor-cloud` 任务、可 attach 的 relay 事件流。</p></div><div class="deliverable pitfalls"><h3>常见坑</h3><p>repo allowlist 默认为空会阻断；`--confirm-allowlist` 会记录 override。</p></div><div class="deliverable verification-command"><h3>验证命令</h3><p>`popola relay &lt;source_cloud_task_id&gt; --dry-run --json | jq '.outcome'`</p></div><div class="deliverable skill-workflow-link"><h3>Skill / Workflow 链接</h3><p><a href="USER_GUIDE.html#hands-off-envelope">Relay / handoff</a></p></div></div>
</section>

<section id="cli-preferences-wizard" class="demo-scenario">
  <h2>CLI preferences wizard</h2>
  <figure class="scenario-figure"><img src="../assets/img/demos/cli-preferences-wizard.svg" alt="CLI preferences wizard 占位图"></figure>
  <p>`popola init --interactive` 的 Step 6 记录可重复 dispatch 偏好，之后命令显式 opt in。</p>
  <details class="command-flow"><summary>命令流</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="zh-hint-prefs">Step 6 只填路由默认值，不填 secret。</span>
<span class="cmd-line" aria-describedby="zh-hint-prefs">popola init --interactive</span>
<span class="cmd-line" aria-describedby="zh-hint-prefs"># Step 6/6: User preferences</span>
<span class="cmd-line" aria-describedby="zh-hint-prefs"># 1. Select default CLI: cursor</span>
<span class="cmd-line" aria-describedby="zh-hint-prefs"># 2. Select default workspace: $(pwd)</span>
<span class="cmd-line" aria-describedby="zh-hint-prefs"># 3. Prefer streaming attach? yes</span>
<span class="cmd-line" aria-describedby="zh-hint-prefs"># 4. Confirm before cloud dispatch? yes</span>
<span class="cmd-line" aria-describedby="zh-hint-prefs"># 5. Save profile name: daily-driver</span>
<span class="cmd-line" aria-describedby="zh-hint-prefs">popola dispatch "summarize the repo" --use-preferences</span></code></pre>
  </details>
  <div class="scenario-deliverables"><div class="deliverable event-sequence"><h3>预期事件序列</h3><p>wizard 写入 `[user_preferences]`，选择 profile，dispatch 展开默认值，任务发出正常本地事件。</p></div><div class="deliverable pitfalls"><h3>常见坑</h3><p>不要把 API key 写进 preferences；凭据放 keyring 或 `CURSOR_API_KEY`。</p></div><div class="deliverable verification-command"><h3>验证命令</h3><p>`popola doctor --json | jq '.user_preferences'`</p></div><div class="deliverable skill-workflow-link"><h3>Skill / Workflow 链接</h3><p><a href="USER_GUIDE.html#用户偏好v0910">用户偏好</a></p></div></div>
</section>

<section id="multi-cli-relay" class="demo-scenario">
  <h2>多 CLI relay（Cursor → Claude → Codex）</h2>
  <figure class="scenario-figure"><img src="../assets/img/demos/multi-cli-relay.svg" alt="多 CLI relay 占位图"></figure>
  <p>Envelope chain 让每个 CLI 保持干净子进程，同时保留可审计路径。</p>
  <details class="command-flow"><summary>命令流</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="zh-hint-multi">用 replay id 避免复制长 prompt。</span>
<span class="cmd-line" aria-describedby="zh-hint-multi">popola dispatch "design the cache migration" --cli=cursor --wait</span>
<span class="cmd-line" aria-describedby="zh-hint-multi">popola handoff list</span>
<span class="cmd-line" aria-describedby="zh-hint-multi">popola dispatch --replay &lt;cursor_handoff_id&gt; --cli=claude --wait</span>
<span class="cmd-line" aria-describedby="zh-hint-multi">popola handoff list</span>
<span class="cmd-line" aria-describedby="zh-hint-multi">popola dispatch --replay &lt;claude_handoff_id&gt; --cli=codex --cli-flag sandbox=read-only</span></code></pre>
  </details>
  <div class="scenario-deliverables"><div class="deliverable event-sequence"><h3>预期事件序列</h3><p>Cursor 产生设计信封，Claude replay 进入实现评审，Codex 用只读 sandbox replay Claude 信封。</p></div><div class="deliverable pitfalls"><h3>常见坑</h3><p>replay 错信封会造成角色漂移；派发前先看 `target_cli` 和 summary。</p></div><div class="deliverable verification-command"><h3>验证命令</h3><p>`popola handoff list --json | jq '.[0].handoff_id'`</p></div><div class="deliverable skill-workflow-link"><h3>Skill / Workflow 链接</h3><p><a href="USER_GUIDE.html#hands-off-envelope">Workflow 2 / hands-off envelope</a></p></div></div>
</section>

<section id="daemon-doctor-fix" class="demo-scenario">
  <h2>Daemon doctor + fix</h2>
  <figure class="scenario-figure"><img src="../assets/img/demos/daemon-doctor-fix.svg" alt="Daemon doctor fix 占位图"></figure>
  <p>演示或 CI smoke 前，用 doctor 走一条红到绿的短路径。</p>
  <details class="command-flow"><summary>命令流</summary>
  <pre class="terminal-block terminal-block--active"><code><span id="zh-hint-doctor">按 doctor 提示修失败子系统，再重跑。</span>
<span class="cmd-line" aria-describedby="zh-hint-doctor">popola doctor</span>
<span class="cmd-line" aria-describedby="zh-hint-doctor"># ✗ popolad: socket missing</span>
<span class="cmd-line" aria-describedby="zh-hint-doctor">popola popolad start</span>
<span class="cmd-line" aria-describedby="zh-hint-doctor">popola doctor --json</span>
<span class="cmd-line" aria-describedby="zh-hint-doctor"># ✓ all checks</span></code></pre>
  </details>
  <div class="scenario-deliverables"><div class="deliverable event-sequence"><h3>预期事件序列</h3><p>Doctor 报一个红色子系统，fix 命令启动 daemon，重跑后所有检查变绿。</p></div><div class="deliverable pitfalls"><h3>常见坑</h3><p>陈旧 socket 可能像 daemon 失败；如果 start 报已有 daemon，先 stop。</p></div><div class="deliverable verification-command"><h3>验证命令</h3><p>`popola doctor --json | jq '.overall'`</p></div><div class="deliverable skill-workflow-link"><h3>Skill / Workflow 链接</h3><p><a href="USER_GUIDE.html#cli-速查">Health + diagnostics</a></p></div></div>
</section>

Generated 2026-05-11 against PopolaLoom v1.0.0-pre.1. For walkthroughs with full output, see [DEMO.md](DEMO.html).
