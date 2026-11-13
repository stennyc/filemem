# filemem 安装提示词（喂 agent）

> 把下面整段贴给 agent。它假设 agent 有 shell + 文件读写能力，且当前宿主是 Hermes
> （`$HERMES_HOME` 已设或默认 `~/.hermes`）。脚本 `install.sh` 已随本仓分发，agent 只
> 需按步骤执行 + 验收，不自行改脚本。

---

## 安装 filemem 到当前 Hermes 宿主

请完成以下 5 步，每步验收后才进下一步。不要跳过验收。

### 0. 前置检查
```bash
echo "HERMES_HOME = ${HERMES_HOME:-$HOME/.hermes}"
[ -d "$HERMES_HOME/profiles" ] && echo "profiles dir: yes" || echo "profiles dir: no (single-profile host)"
[ -f "$HERMES_HOME/config.yaml" ] && grep -E '^\s*multiplex_profiles:' "$HERMES_HOME/config.yaml" || echo "no multiplex_profiles key (host not multiplexed)"
```
- 若宿主是 multiplexed（顶层 `config.yaml` 有 `multiplex_profiles: true`），继续走 1–5。
- 若宿主非 multiplexed（单 profile），只走 1、3、5（不需要 `--all-profiles`）。

### 1. 装代码面（`install.sh` 默认 dir 模式）
```bash
cd <filemem 仓>    # 本仓 checkout，或 git clone https://github.com/stennyc/filemem.git
bash install.sh    # 幂等；自动备份旧版、保留 storage/、写 $HERMES_HOME/config.yaml 的 memory.provider
```
**验收**：脚本末行打出 `[dir] install complete`，且 `$HERMES_HOME/config.yaml` 含 `memory: provider: filemem`。

### 2. （可选，仅 117 这类用 EP 的部署）装 pip 代码面
```bash
FM_TARGET_PYTHON=$HERMES_HOME/tools/python-3.14*/bin/python3.14 bash install.sh --pip
```
**验收**：`[pip] VERIFIED — entry point registered`。

### 3. 全部 profile 启用（multiplexed 宿主必做；owner 可排除）
```bash
# 默认：遍历所有 profiles/*/config.yaml
bash install.sh --all-profiles

# owner 约束（本机场景）：那 4 个 profile 不要动
FM_EXCLUDE=4d,amoi,culinary,default bash install.sh --all-profiles
```
- 遍历 `$HERMES_HOME/profiles/*/config.yaml`，每个都写入 `memory.provider: filemem` + `plugins.entries.filemem.allow_tool_override: false`（幂等 merge，不覆写已有段）。
- **`FM_EXCLUDE`**：逗号分隔的 profile 名，这些 profile 被跳过（owner 约束：那 4 个 profile 不装）。
- **`FM_INCLUDE`**：可选，逗号分隔；设了它就**只**装这些 profile（INCLUDE 优先于 EXCLUDE）。
- 顶层 `config.yaml` 若缺 `multiplex_profiles: true`，脚本会 **WARN 但不 auto-set**（那是 gateway 拓扑键，不是 provider 配置；installer 必须不静默改路由）。看到 WARN 就手动在顶层 `config.yaml` 加 `multiplex_profiles: true` 再重启。

**验收**：对每个**要装**的 profile（不在 FM_EXCLUDE 里）跑一次：
```bash
grep -E '^\s*provider:\s*filemem' $HERMES_HOME/profiles/*/config.yaml
grep -E '^\s*filemem:' $HERMES_HOME/profiles/*/config.yaml
```
被装的那几个 profile 全 hit；FM_EXCLUDE 里的那 4 个**不**hit（保持原样）。

### 4. 重启 gateway（一次性）
```bash
# 停 host 的 gateway（multiplexed 宿主只需停一次，所有 profile 共享一个进程）
pkill -f "hermes.*gateway"
# 起回（原 cmdline；本宿主通常是 dashboard --port 9119/9200）
# 按宿主原有方式启动；重启后 filemem 对每个 profile 都生效
```
**验收**：`grep -i filemem $HERMES_HOME/logs/*.log | tail -5` 看到每个 profile 的 `Memory provider 'filemem' activated`。

### 5. 冒烟（跨 profile 验证）
- 在任意一个 profile 的 chat 里写一条事实，切到**另一个** profile，问它同一事实。
- 期望：第二个 profile 也能召回（中立根 `<base>/filemem` 共享，非 per-profile）。

---

## 提示词（直接喂 agent 的原文）

```text
你是我的安装执行者。按 INSTALL_PROMPT.md 第 0–5 步装 filemem。

硬性约束：
1. 所有步骤在 shell 里跑 install.sh，不要自己手写 config.yaml 的 yaml 合并逻辑。
2. install.sh --all-profiles 是幂等的：对已启用的 profile 会打出 "already set — skip"，
   看到 skip 就是正确行为，不要再改。
3. 顶层 multiplex_profiles 键：installer 不会 auto-set（它是 host 拓扑）。
   如果 WARN 提示缺失，你必须停下来问我，不要自己改顶层 config.yaml。
4. 验收以盘上真值为准（grep 出的行 / 日志里的 activated 行），不要凭"脚本没报错"
   就报成功。
5. 每步验收全绿才进下一步；某步红了停下来报给我。
6. 若 $HERMES_HOME/profiles 不存在（单 profile 宿主），跳过 --all-profiles，
   直接走 1、3、5。

执行顺序：0 → 1 → 2(可选) → 3 → 4 → 5。每步末打一行 PASS/FAIL 给我。
```
