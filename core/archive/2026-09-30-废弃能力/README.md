# 废弃能力归档 · 2026-09-30（平台侧）

从 `plugins/` 挪出来的三个**已停用**插件。它们对应的 `config.yaml` 条目已经**注释掉**
（注释里写了归档位置），所以内核不再拉起它们。

| 插件 | 原来干什么 | 为什么归档 |
|---|---|---|
| `inbox_plugin.py` | 投递箱：盯 vault，新增/变化的文件自动索引进库 | 2026-09-27 库正式启用后 vault 转只读参考，投递箱停用；与笔记库侧的镜像管线同命运（那边也归档了） |
| `clock_plugin.py` | 示例时钟插件（发 clock.tick / clock.hourly） | 示例代码，一直 `enabled: false`，没有消费者 |
| `demo_heartbeat.py` | `heartbeat-A`：只测跨进程监督机制 | 测试用，不参与功能 |

**要取回**：把对应 `.py` 放回 `plugins/`，在 `config.yaml` 里取消那段注释，
然后 `POST http://127.0.0.1:8790/api/core/restart`（或控制台点重启内核）。

**注意**：`inbox_plugin.py` 里还在调 `/api/mirror/sync`，而那条接口 2026-09-30 起**已退役（回 410）** ——
真要恢复投递箱，得先把笔记库侧的镜像管线一起恢复（见 `D:\core-notes\archive\2026-09-30-废弃能力\README.md`）。
