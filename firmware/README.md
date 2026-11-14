# ESP32-C3 0.8.9

当前固件已移除蓝牙，只保留 WiFi 配网、局域网探测与 WOL 唤醒。已关联 WOL关机服务的电脑由服务判断在线，网关不再周期探测它们，保留开机唤醒。

0.8.0 增加双槽远程OTA、签名与首启回滚。注册和心跳继续上报WiFi SSID，不上报密码。首次需通过USB迁移分区，保留NVS中的配网及绑定数据；以后在网关编辑弹窗上传本站固件ZIP。

0.8.1 在网关每次取得 Wi-Fi 地址后立即重启 SNTP 请求；校时超过 90 秒仍未完成时，配网页显示时间同步失败，固件保留自动重试，校时成功后自动继续 MQTT 接入。浏览器不再把校时阶段误报为配对服务器超时。

## 保留的局域网扫描逻辑

扫描改为两轮：第二轮仅补扫尚未发现的地址。每批未响应地址重试，等待窗口从300+300ms改为300+700ms，每50ms提取响应，减少ARP缓存挤出造成的遗漏。发送使用etharp_query预留待解析记录，发现结果按IP/MAC去重，快速响应设备无需等满窗口。

保留90秒总预算、取消/前台扫描抢占与网络断开检查。默认仍扫描网关所在/24（或设置的合法局域网范围，最多254个主机）；跨网段、访客隔离、休眠设备不保证发现。后台候选列表隐藏已添加设备，最终提示分别显示发现/已添加/可添加数量。最低兼容固件仍0.5.1。

0.8.3 增加配网页扫描总超时、重复配网提交拦截及同名不同加密网络选择修复；配网和服务器接入只原子更新对应字段，过期接入响应不覆盖新配对码、网络配置及设备列表。增加硬件不匹配和固件版本不支持的明确提示。欠压、看门狗和软件复位不参与连续上电恢复出厂计数，防止供电不稳误清配置。

0.8.5 修复曾联网后断开、暂停重试却继续显示正在连接的问题；接口与页面立即显示失败并恢复网络配置。保留重新打开页面的25秒等待期限、迟到响应隔离、前台恢复检查及检测域名跳转至192.168.4.1。

0.8.7 局域网访问设备IP的根路径不再显示“Connect to the setup Wi-Fi first”，改为空404；配网热点访问与API来源限制保持不变。

0.8.8 配网页增加默认收起的高级设置，可自定义 MQTT broker 地址；留空使用默认地址。Wi-Fi 重配和服务器配对不会覆盖自定义地址。

0.8.9 将本机固件工具链迁移到 ESP-IDF 6.1 / Python 3.15.2，锁定 MQTT 与 cJSON 托管组件，SHA256 使用 PSA Crypto。指令摘要协议、OTA 签名公钥、分区布局及 WiFi 功率保持原样。

0.8.6 增加WiFi扫描信道停留时间，本轮未发现已保存网络时追加一次定向补扫。最多保留32项最近60秒的扫描结果，旧结果注明发现时间，不能据此判定设备在线。配网20秒超时单独反馈，不再将主动断开的错误36解释为密码错误；区分服务器未响应与网关未能建立安全连接，并记录不含凭据的传输错误。保持原有自动重试和证书验证。

包esp32c3-0.8.6.zip含版本对应的USB刷写脚本和签名OTA元数据，同一个包用于USB首次迁移与网页升级。实际验证和部署结果见docs/WORK_LOG.md。

## Build and flash

Target: ESP32-C3, 4 MB flash, ESP-IDF 6.1 and Python 3.15.2. Current firmware: 0.8.9. Older supported firmware retains LAN features, but requires USB migration before OTA.

Install ESP-IDF 6.1 and Python 3.15.2, then run `C:\Python315\python.exe firmware/build.py --install-python-environment` from the project root for the first build. Subsequent builds use `C:\Python315\python.exe firmware/build.py`. The script defaults to `C:\esp\v6.1\esp-idf`, tools root `C:\Espressif` (which contains `tools/`), and `C:\Python315\python.exe`; override `--idf-path`, `--tools-path`, or `--python` for another installation. The official `idf_tools.py` environment installer creates a dedicated Python 3.15 environment; its existing Python 3.11 environment is retained. The build entry point invokes the selected interpreter directly instead of relying on shell aliases. `build.ps1` remains an optional wrapper on computers where PowerShell scripts are permitted. This migration covers local tools and firmware only; the website server's Python runtime is unchanged.

Output and generated SDK configuration are isolated under `firmware/build-idf61/`; the previous `firmware/build/` and root `sdkconfig` are not reused. Commit `dependencies.lock` with the managed `espressif/cjson` and `espressif/mqtt` dependencies to reproduce the resolved versions. Use `-Action size` to inspect memory and `-Action flash -Port COMx` to build and flash after identifying the board. Open the firmware folder separately in VS Code for ESP-IDF extension commands; select the matching IDF 6.1 setup.

IDF 6.1 uses PSA Crypto for SHA-256. MQTT command digest inputs and the OTA ECDSA signature format remain unchanged; TLS and public-key parsing continue through the supported mbedTLS APIs. The OTA partition layout, NVS storage, reset guard and Wi-Fi power setting are unchanged. A successful build does not establish network, power-supply or OTA rollback reliability; those require hardware verification.

Create a signed private firmware ZIP with `python tools/package_firmware.py --build-directory firmware/build-idf61` from the root. The signing key is required and must stay outside source control. The packager checks the build's application version and chip and records the actual IDF revision in its README.

For USB flashing without rebuilding, extract the latest `deploy/esp32c3-0.8.9.zip`, open an ESP-IDF terminal in the extracted directory, and run its included versioned Flash script with `-Port COMx`. Verify MAC and back up full flash first. Offsets: bootloader 0x0, partition table 0x8000, blank otadata 0x10000, application 0x20000. NVS 0x9000–0xefff is preserved. The initial wired migration requires uninterrupted power and USB recovery if interrupted.

## Remote OTA

Requires an online healthy gateway, matching OTA layout and the independent `gateway_ota` permission. Upload the signed ZIP, never a raw bootloader or partition table. ECDSA P-256 verifies the manifest, and image size/chip/project/version/SHA256 are checked before switching the non-running slot. This is application verification, not hardware Secure Boot; no eFuses are changed. The signing private key stays in the local private directory and must be preserved for future releases.

Select gateways in the website list to run a batch upgrade. The signed package is checked before confirmation; only older supported versions are upgraded. Same/newer versions, unavailable gateways and conflicting tasks are listed with skip reasons. The server rechecks ownership, permissions, frozen configuration revisions and the entire plan before dispatch. Each gateway keeps its own progress and result. A single-gateway upgrade can reinstall the same version.

Downloads use a fixed certificate-validated HTTPS endpoint with an expiring per-task bearer token and a 4KiB RAM buffer and 32KiB authenticated ranges. Exact range/length checks and bounded retries recover transient disconnects within a running task. Redirects are rejected. Scanning and conflicting configuration changes are blocked during upgrade. New applications must pass bounded startup checks before being marked valid; a crash before confirmation triggers bootloader rollback. Same-version reinstall is allowed; downgrades are rejected. The first USB base image has no previous valid application to roll back to.

## Network and pairing

An unconfigured device opens `WOL-Setup-<last six MAC digits>`; connect and open `http://192.168.4.1`. Select a nearby 2.4 GHz Wi-Fi network, enter its password, and use the one-time pairing code generated by the dashboard. Wi-Fi cards show signal strength and security; unsupported authentication modes are disabled.

Credentials are automatically enrolled over certificate-validated HTTPS using the pairing code or existing binding key. MQTT uses TLS on port 8883 and a device-scoped account. No anonymous MQTT login or manual credential entry is needed. Existing Wi-Fi and pairing data are retained on ordinary firmware updates.

The setup page shows a loading indicator while reading status; failures remain visible. Different SSIDs can share one bridged LAN, but guest/AP isolation or separate broadcast domains can prevent discovery even when IPv4 addresses have the same prefix.

## Current delivery

本机COM7（EDF0CC）已保留NVS更新至0.8.9；WiFi、校时及MQTT正常，并完成一次同版本签名OTA：下载、摘要与签名校验、切槽、重启自检均成功。USB初次验证保留原bootloader；本轮未验证新bootloader的首次安装，也未验证长期供电稳定性。COM4（DAAB18）未写入，不能远程升级其旧分区布局。其他硬件的实际联网、远程升级和回滚验证结果以`docs/WORK_LOG.md`最新记录为准。

Obsolete release packages and build caches are cleaned after validation. Pending recovery backups and fault evidence remain privately under `.local` and are excluded from release archives.
