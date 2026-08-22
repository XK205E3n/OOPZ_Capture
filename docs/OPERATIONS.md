# 运行与故障处理

## 正常启动

1. 运行 `启动OOPZ全流程.bat`；
2. 等待控制器窗口、NapCat 和 OneBot 就绪；
3. 确认管理员收到“机器人已启动”和帮助；
4. 私聊发送 `/oopz 状态`；
5. 发送 `/oopz 开始` 并按名称选择域和频道。

不要同时运行第二个控制器。单实例锁会拒绝重复启动。

## 正常结束

发送 `/oopz 离开`。程序会等待当前分片安全结束、合并转写、自动修复失败分片，再询问是否分析。不要直接关闭控制器窗口，否则其进程内的录音任务也会终止。

## 状态检查

```powershell
.\.venv\Scripts\oopz-qq-controller.exe state
.\.venv\Scripts\oopz-onebot.exe state
.\.venv\Scripts\oopz-qq-watchdog.exe check
.\.venv\Scripts\oopz-continuous.exe status --session "SESSION_ID"
```

QQ `/oopz 状态` 优先读取实际生命周期，不应只依赖控制器的缓存状态。

## 常见故障

### OneBot 拒绝连接

表示 `127.0.0.1:3001` 没有监听。先确认 NapCat 是否登录并已启用正向 WebSocket。网关会退避重连，自愈监视器在宽限期后尝试恢复。

### QQ 端口正常但机器人不收消息

检查 `onebot_gateway.json` 的 `connected` 和 `qq_send_available`。网关即使没有待发消息也会定期调用 OneBot `get_status`；账号被踢下线时端口可能仍正常，监视器会重启 NapCat saved session，仍失败时打开交互登录。不能绕过腾讯要求的扫码或验证。

执行 `一键重启OOPZ全流程.bat` 时，程序会在停止网关前调用 OneBot 登录诊断。诊断确认 NapCat 与 QQ 均可用时，NapCat/QQ 进程不会被停止，避免无意义地刷新登录会话；只重启控制器、OneBot 网关和看门狗。若端口未监听、诊断超时或 QQ 已离线，则把 NapCat 视为异常并进入原有恢复流程。诊断结果记录在 `controller_state/logs/napcat-restart-health.log`。

`KickedOffLine` 表示 QQ/NapCat 登录态已经被服务端判定失效，不是 OOPZ 录音异常。程序只能检测、重启和引导重新认证，不能安全绕过 QQ 风控。检查手机 QQ 的登录设备列表，避免机器人账号同时由另一台 PC 或另一份 NapCat 登录；需要人工认证时使用 `NapCatQQ/NapCat.50969.Shell/cache/qrcode.png` 中最新生成的二维码。

首次稳定运行后执行根目录的 `配置NapCat自动登录.bat`。它把密码计算为登录所需的 MD5 摘要，再通过 Windows DPAPI 绑定到当前 Windows 用户和本机保存；磁盘上不保存 QQ 明文密码或明文摘要。以后看门狗恢复顺序为“历史会话快速登录 → 加密凭据密码回退 → 腾讯安全验证/二维码”。更换 Windows 用户、重装系统或迁移电脑后需要重新配置。该回退只能减少因本地票据失效造成的扫码，无法绕过腾讯的新设备验证、验证码或账号风控。

NapCat 基于 Windows NTQQ，不提供受支持的“iPad 协议”切换。旧协议框架中的设备类型伪装不会继承本机 NTQQ 登录态，可能反而触发设备变更风控，因此本项目不采用。机器人账号应只由这一份 NapCat/NTQQ PC 客户端登录；避免另一台 PC、另一份 NapCat、VPN/代理切换、频繁重启和系统休眠。手机 QQ 可保留用于接收官方安全验证，但不要反复删除本机登录设备或重置 NapCat Device GUID。

### 附件发送失败

文本与附件请求持久化在 `controller_state/send_requests`。PDF/Markdown 普通附件使用 NapCat 的 `upload_private_file` / `upload_group_file` 专用接口，不再包装成富消息 file segment；附件使用有限次数重试，最终失败会私聊通知管理员，普通文本在短暂 QQ 故障期间继续保留。

### 转写分片失败

控制器默认自动重试一轮。若结束通知仍显示失败：

```powershell
.\.venv\Scripts\oopz-continuous.exe repair --session "SESSION_ID"
```

失败音频不会自动删除。修复成功后会重新合并总转写与分析 handoff。

### 分析失败

失败状态会返回 Session 和异常摘要。检查 API 配置、限流及网络后，用 `/oopz 待分析` 重新选择。分析器有 Session 级锁，重复请求不会并行覆盖同一报告。

## 变更配置

先发送 `/oopz 设置状态`，再使用统一格式：

```text
/oopz 设置 变量名=值
```

录音相关设置在下一次开始录音时生效；API 设置在下一次分析时生效。秘密值回显时只显示已设置和长度。

报告选择、转发类型和目标号码的等待状态只保存为小型 JSON，不会调用 API 或占用录音/分析进程。默认3分钟超时，控制器每5秒清理一次；超时后自动跳过并给对应管理员发送提示。

## 保留清理

先预览，再执行：

```powershell
.\.venv\Scripts\oopz-worker.exe cleanup --output-root "D:\Codex\OOPZ_Capture\output" --dry-run
.\.venv\Scripts\oopz-worker.exe cleanup --output-root "D:\Codex\OOPZ_Capture\output"
```

只会删除带合法受管生命周期且已到 `delete_after` 的顶层 Session，以及其清单登记的 PDF。发现链接或 reparse point 时拒绝删除。
