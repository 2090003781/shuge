# 废弃能力归档 · 2026-09-30

这些能力都**不再参与运行**，代码从主树挪到这里保存（不删，随时可取回）。
为什么归档、取回要动什么，逐条写在下面。

> 立此存照的体检结论（详见库内《体检报告》第六、九节）：这些能力要么被更晚的设计取代，
> 要么依赖一个已经关掉的通道，留着只会增加维护面与误触面。

## 1. 镜像管线（库文件夹 ⇄ vault 双向同步）

- **为什么废弃**：2026-09-27 辞玖正式启用库，明确「**库是唯一真相源**」，
  vault（`D:\个人数据\辞玖`）转为只读参照，镜像总开关 `mirror_enabled=0` 之后一直没再开。
- **归档了什么**
  - `mirror.py`（同步实现本体）
  - `tools/smoke_mirror.py`（它的自测）
  - `tools/acc_mirror.py`、`tools/acc_tree_write.py`（依赖它的验收脚本）
- **主树里同时改了**
  - `POST /api/mirror/sync` **退役**：现在回 `410 Gone` + `{"retired": true}`（比 404 说得清楚）。
  - `server.py` 里的 `mirror_run()` / `library_orphans()` 一并删掉。
  - `GET /api/mirror` 与 `POST /api/mirror/config` **保留** —— 它们其实是
    「库文件夹 / vault 路径」的**配置入口**（`library_dir` / `vault_dir`），
    临时库测试、`acc_write_through`、`smoke_name_guard` 都要用它。名字里的 mirror 是历史遗留。
- **要取回**：把 `mirror.py` 放回 `src/`，恢复 `server.py` 里的 `mirror_run` + 那条 POST 路由，
  并把 `tools/` 里三个脚本放回去。**注意**：重开之前先想清楚库文件夹的形态口径 ——
  现在它同时存在「vault 原件形态」（171 篇，镜像时代抄的）和「库视图形态」（1111 篇，库 write_through 写的），
  见 `tools/materialize.py --verify` 的三方判别。

## 2. 三个已停用的插件（`core/plugins/`）

- `inbox_plugin.py`（投递箱：把 vault 的改动镜像进库）—— 与镜像管线同命运；`enabled: false`。
- `clock_plugin.py`（示例：定时心跳）—— 示例代码，`enabled: false`。
- `demo_heartbeat.py`（`heartbeat-A`：只测监督机制）—— 测试用，`enabled: false`。
- **配置**：`config.yaml` 里对应的三条已经**注释掉**并注明归档位置。
- **要取回**：把文件放回 `plugins/`，在 `config.yaml` 里取消注释，重启内核（`POST :8790/api/core/restart`）。

## 3. `tools/lock_force_reset.py`（破窗：忘了库密码时清掉密钥材料）

- **为什么归档**：它能**在不知道密码的情况下废掉所有私密篇**（清 `kdf_*` / `verify_blob`），
  是整条链上唯一致命的单点。它故意不在界面上，但放在常用 `tools/` 里迟早会被手滑点到。
- **要取回**（真忘了密码时）：`python archive/2026-09-30-废弃能力/lock_force_reset.py --yes`，
  然后到页面上重新设一个新密码；已经加密的私密篇要用 `POST /api/maintenance/private-rekey` 从磁盘明文捞回来。
- **注意**：它和以前一样需要磁盘权限，且**不可逆**。
