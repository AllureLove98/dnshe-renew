# DNSHE 自动续期

该项目每天通过 DNSHE Free Domain API 尝试续期，并将中文执行报告推送到 Bark。报告包含执行时间、处理范围、成功/未开放/跳过/失败统计，以及每个域名的详细处理结果。续期尚未开放会显示为“暂未开放续期”，不会使工作流失败；其他 API 失败会使工作流失败并以 critical 级别通知。

## GitHub 配置

在仓库 **Settings → Secrets and variables → Actions** 中添加：

| 类型 | 名称 | 值 |
| --- | --- | --- |
| Secret | `DNSHE_API_KEY` | DNSHE API Key |
| Secret | `DNSHE_API_SECRET` | DNSHE API Secret |
| Secret | `BARK_URL` | 完整 Bark 地址，例如 `https://api.day.app/你的设备密钥` |
| Variable（可选） | `DNSHE_DOMAINS` | 仅续期指定完整域名，逗号分隔；留空则处理 API Key 名下所有子域名 |

将代码推送到 GitHub 后，在 **Actions → DNSHE domain renewal → Run workflow** 手动运行一次验证。手动运行时可填写一个或多个完整域名（英文逗号分隔），留空则处理全部；手动输入会优先于 `DNSHE_DOMAINS` Variable。之后会在每天 **08:15（北京时间）** 自动运行一次。可在 [`.github/workflows/dnshe-renew.yml`](.github/workflows/dnshe-renew.yml) 修改 Cron；GitHub 的定时任务有时会有排队延迟。

## 本地测试

PowerShell：

```powershell
$env:DNSHE_API_KEY = 'cfsd_xxx'
$env:DNSHE_API_SECRET = 'xxx'
$env:BARK_URL = 'https://api.day.app/你的设备密钥'
python .\renew.py
```

不要把密钥写入仓库或 `.env` 文件。DNSHE 文档要求通过 `X-API-Key` 和 `X-API-Secret` 请求头鉴权；脚本遵循这一方式。
