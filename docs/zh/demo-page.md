---
layout: default
title: 演示页
description: PopolaLoom 视觉化 walkthrough：选择场景，查看精确命令流。
lang: zh
translation_url: /demo-page.html
---

<!-- updated: 2026-05-10 -->

<section class="hero hero--small">
  <h1>PopolaLoom 演示页</h1>
  <p class="tagline">选一个场景，照着命令跑，再 attach 到织机的事件流。</p>
  <p><a href="../index.html">返回文档首页</a></p>
</section>

## 场景选择

<div class="scenario-grid">
  <a class="scenario-card" href="#local-single-cli"><span class="scenario-card__badge">v0.1.0+</span><h3>本地单 CLI（Cursor）</h3><p>让 Cursor 在本机跑一个任务，`popolad` 负责跨终端保留事件流。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#cross-cli-handoff"><span class="scenario-card__badge">v0.7.0+</span><h3>跨 CLI handoff</h3><p>把 prompt 写成 Markdown 信封，检查之后交给另一个 CLI 继续。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#hitl-pause"><span class="scenario-card__badge">v0.4.1+</span><h3>HITL 暂停 + Lark 审批</h3><p>任务暂停等待人工判断，五个通道争抢同一个原子回答。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#cloud-agent"><span class="scenario-card__badge">v0.8.5+</span><h3>Cloud Agent 派发</h3><p>使用 Cursor Cloud runtime，同时保留 `status`、`attach` 和 cancel 语义。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#self-hosted-worker"><span class="scenario-card__badge">v0.9.1+</span><h3>Self-hosted worker handoff</h3><p>把这台机器注册成 Cursor worker，再选择 dashboard 或 PopolaLoom 来创建 run。</p><span class="scenario-card__link">查看流程</span></a>
  <a class="scenario-card" href="#cross-pr-relay"><span class="scenario-card__badge">v0.8.8+</span><h3>跨 PR relay</h3><p>把一个 cloud task 的产物变成另一个 repo-aware cloud task 的输入。</p><span class="scenario-card__link">查看流程</span></a>
</div>

<section id="local-single-cli">
  <h2>本地单 CLI（Cursor）</h2>
  <p>最小路径证明 PopolaLoom 是持久任务总线，而不是 shell alias。Cursor 子进程挂在 `popolad` 下，任意终端都可以重新 attach 同一份 NDJSON 事件流。</p>
  <pre class="terminal-block terminal-block--active"><code># 安装并注册 Skill
./install.sh install
popola init cursor --global

# 启动边车并派发本地任务
popola popolad start
popola dispatch "echo hello from popola" --cli=cursor

# 复制返回的 task id，订阅事件流
popola attach &lt;task_id&gt; --follow</code></pre>
  <p>你会看到 `process.stdout`、`state.*` 和最终 `task.completed`；`Ctrl-C` 只退出 attach。</p>
  <p><a href="../USER_GUIDE.html#task-lifecycle">阅读深挖</a></p>
</section>

<section id="cross-cli-handoff">
  <h2>跨 CLI handoff（Cursor → Claude）</h2>
  <p>每次 dispatch 都先写 Markdown front-matter 信封，再构造 adapter argv。这个文件就是交接审计记录，可以展示、归档、replay，也可以作为下一个 CLI 的上下文来源。</p>
  <pre class="terminal-block terminal-block--active"><code>popola dispatch "fix the NoneType bug in foo.py" --cli=cursor
popola handoff list
popola handoff show &lt;handoff_id&gt;

popola dispatch "review the cursor fix and propose follow-up tests" --cli=claude --cwd "$(pwd)"
popola attach &lt;claude_task_id&gt; --follow</code></pre>
  <p>信封 id 是 slug-hash 稳定的；第二个任务是独立子进程，有自己的事件日志。</p>
  <p><a href="../USER_GUIDE.html#hands-off-envelope">阅读深挖</a></p>
</section>

<section id="hitl-pause">
  <h2>HITL 暂停 + Lark 审批</h2>
  <p>LangGraph 节点调用 `interrupt()` 后，daemon 会把同一个 HITL 请求发到所有可用通道。Lark 不存在时，本地 CLI 和 NDJSON 状态仍然可用。</p>
  <pre class="terminal-block terminal-block--active"><code># 终端 A：观察任务暂停
popola attach &lt;task_id&gt; --follow

# 终端 B：从 CLI 通道查看并回答
popola pending
popola feedback hitl-abc12 yes --reason "verified backup taken"

# 终端 A：看到 state.resumed
popola status &lt;task_id&gt; --json</code></pre>
  <p>预期顺序是 `task.elicited`、一个获胜回答、`state.resumed`；迟到通道只会看到已回答状态。</p>
  <p><a href="../USER_GUIDE.html#hitl-workflow">阅读深挖</a></p>
</section>

<section id="cloud-agent">
  <h2>Cloud Agent 派发</h2>
  <p>Cloud runtime 把本地 `Popen` 换成 Cursor Background Agent REST，但保留同样的 task id、status、attach 和 cancel 形状。先配置凭据，再用 `--cli=cursor-cloud` 派发。</p>
  <pre class="terminal-block terminal-block--active"><code>./install.sh install --with-credentials
popola auth cursor set --validate

popola popolad start
popola dispatch "Plan database migration scaffolding" \
  --cli=cursor-cloud \
  --cli-flag repo_url=https://github.com/acme/repo

popola attach &lt;cloud_task_id&gt; --follow</code></pre>
  <p>`popola list` 会显示 `runtime=cloud`；attach 优先读 Cursor SSE，stream 过期后退回 poll。</p>
  <p><a href="../USER_GUIDE.html#cloud-agent-dispatch-v085">阅读深挖</a></p>
</section>

<section id="self-hosted-worker">
  <h2>Self-hosted worker handoff</h2>
  <p>`popola cloud worker` 包装 Cursor worker CLI，让这台机器出现在 Cloud Agents UI。`handoff` 不产生副作用；`dispatch` 才会创建 PopolaLoom 可追踪的 cloud task。</p>
  <pre class="terminal-block terminal-block--active"><code>popola cloud worker debug --worker-dir "$(pwd)"
popola cloud worker start --worker-dir "$(pwd)"

popola cloud worker status --management-addr 127.0.0.1:39231 --json
popola cloud worker handoff --worker-dir "$(pwd)" --prompt "Run the migration smoke"
popola cloud worker dispatch "Run the migration smoke" --worker-dir "$(pwd)" --print-only</code></pre>
  <p>同一 `--worker-dir` 的重复 start 默认复用已有 worker；明确需要两个 worker 时才传 `--allow-duplicate`。</p>
  <p><a href="../USER_GUIDE.html#self-hosted-worker-handoff-popola-cloud-worker-v091">阅读深挖</a></p>
</section>

<section id="cross-pr-relay">
  <h2>跨 PR relay</h2>
  <p>Relay 把一个 cloud task 的结果变成另一个 cloud task 的输入。默认偏自动化，但有 allowlist、审计日志、idempotency 和 secret scan 做约束。</p>
  <pre class="terminal-block terminal-block--active"><code>popola relay &lt;source_cloud_task_id&gt; --dry-run

popola relay &lt;source_cloud_task_id&gt; \
  --target-repo https://github.com/acme/other-repo \
  --confirm-allowlist

popola attach &lt;relay_task_id&gt; --follow</code></pre>
  <p>结果要么被 policy 拒绝，要么产生一个和手动 cloud dispatch 一样可观测的 `cursor-cloud` 任务。</p>
  <p><a href="../USER_GUIDE.html#cross-pr-relay--popola-relay-v088">阅读深挖</a></p>
</section>

Generated 2026-05-10 against PopolaLoom v0.9.7. For walkthroughs with full output, see [DEMO.md](../DEMO.html).
