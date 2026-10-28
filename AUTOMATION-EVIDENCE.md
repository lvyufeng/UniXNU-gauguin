# 自动调试证据通道：实测边界

## 已实测（本次会话）

- 设备 serial d25f844e，TWRP 3.6.2.1B-gauguin，可用 adb。
- boot 与 recovery 134217728 字节，完整 SHA256 均为 `8f488a0a2a766f0a1076a21be0a9f0187b2ee8e598bebd64391236b6d155fb1b`。
- `/dev/pmsg0` 存在，`/dev/mem`、`/proc/last_kmsg` 不存在。
- pstore 已注册 ramoops，物理地址 0xb0000000，大小0x400000，record_size=0。
- 通过 Linux 标准接口向 `/dev/pmsg0` 写入 `UNIXNU_PMSG_RETENTION_TEST_2026_10_28_A`，sync，执行 `adb reboot recovery`。重连后 `/sys/fs/pstore` 为空，没有可取回的 token。这是一次标准恢复重启路径的阴性实验；它不证明所有复位路径都不保留 DRAM，也不证明丢失发生在哪一阶段。
- 实验前/后 dmesg 存于主机 `/tmp/gauguin-automation-baseline-dmesg.txt` 和 `/tmp/gauguin-pmsg-reboot-dmesg.txt`。前 Powerup reason=0x80001，后0x40001；二者均报 PS_HOLD 相关 power-off 信息。未将这些字段解码为 XNU 阶段。

## 不能当作成功的证据

- fastboot Sending/Booting OKAY：仅表示传送/启动命令成功。
- 回到 TWRP：仅证明恢复路径工作；故障、看门狗和主动复位都可能导致。
- 回归时间：没有受控基线和正/负对照时，只是时序观察。
- 空 pstore：没有可采集日志，不代表没有 panic。
- EFI SetVariable：gauguin 固件使用 RAM-only emulated variable storage，不具备已验证的跨复位持久性。

## 自动化分层

1. 主机闭环：镜像哈希、serial验证、只读基线、单次临时boot、有限等待、恢复日志与哈希取回、JSON结果；不写分区。
2. 阶段证据：需要独立的带run-id记录或经过校准的时序对照，才能证明特定XNU探针执行。当前没有可用的持久阶段token或活跃USB console。
3. 内核日志：自动收回TWRP dmesg并不等于收回XNU日志。USB或可靠持久记录打通以前必须明确区分。

## 主机闭环实测

已实现 `scripts/auto-debug.py`，15项注入时钟/执行器测试通过。实际运行一次：TWRP基线核验→bootloader→一次fastboot临时boot→自动等待恢复→自动采集dmesg/recovery.log/cmdline/pstore目录→重新核验boot/recovery哈希。命令退出0，结果 `recovery_returned`、`phase_verified=false`、`partition_hashes_unchanged=true`、errors为空。临时boot完成到确认TWRP约85.09秒；该时长仅为一次运输/恢复观察，不证明MMU阶段或复位原因。结果目录 `/tmp/gauguin-auto-debug/20261028T123915.526013Z-fe04fac9fb37/`。

实际payload config含 `[boot] timeout=3 default="xnu"`，Menu.c倒计时到零会选择默认entry，无需手动点击。`[kits] autoload=false` 仅影响kext自动加载，不是禁止自动启动。

## 移除未验证写入

审计发现 DarwinBoot Entry.S 仍有历史上向0xb0000000等多个猜测ramoops偏移写入假定record的代码。此路径没有持久化实验证据，本轮已用汇编 `.if 0` 禁用整个写入区；不把错误注释当真实协议。当前保留此前已有的看门狗配置（寄存器单位仍需核对，不认同注释中的毫秒/三分钟结论）。已成功构建更安全固件，临时image SHA256 `249b581b1732411ad7564e6d0cf78d567f2b18b4463bfc02a24b7334c34d7994`。实际自动测试也完成：`recovery_returned`、errors为空、分区哈希未变，boot完成到TWRP约85.22秒，结果目录 `/tmp/gauguin-auto-debug/20261028T124703.228306Z-bc697f1eb608/`。这不是MMU后的新探针，只清理已知不可靠的取证副作用。

## 加固后的闭环真机验收

DeepSeek子代理交付 boot-ID、恢复哈希、deadline、采集完整性门。主代理复核并运行全仓库106项离线测试（退出0）后实测一次。

- 结果目录：`/tmp/gauguin-verified-auto-debug/20261028T143609.024127Z-0a9deb08c902/`。
- 镜像：`249b581b1732411ad7564e6d0cf78d567f2b18b4463bfc02a24b7334c34d7994`。
- pre boot ID：`8aec1d8e-76ec-4ad8-83a9-7976bdb8e605`。
- 恢复 boot ID：`3ca04640-d01a-404b-8981-b399cad8755b`，不同于pre。
- `collection_verified=true`、`recovery_boot_verified=true`、`transport_verified=true`、`partition_hashes_unchanged=true`、errors/warnings均为空。
- 实际只发1次fastboot boot；boot命令完成到新TWRP确认85.3945秒。
- `phase_verified=false`，未把上述恢复证据冒充XNU/MMU阶段证据。

## 持久日志边界与实际内核核验

sysfs实读：ramoops.console_size=2097152、pmsg_size=2097152、mem_address=2952790016、mem_size=4194304、record_size=0、ftrace_size=0、ecc=0。标准pmsg源码接受字节，并不自动证明当前gauguin厂商write实现与上游相同。已检查recovery init/路径未发现搬走pstore的证据，但未审所有vendor服务，不能绝对否定。

`/dev/block/sda15`由TWRP cmdline block2mtd绑定为mtd0，历史块含Linux4.19日志；不是XNU日志，无run-token不得推断XNU执行。raw前8字节为uint32 seq加magic `0x5d005d00`，解析器按真实布局验收，禁止写该分区。

主代理从实际payload FAT簇链读取内核，核对SHA256 `6cc5894a917d0a93c117ace3338eda493b1a93cd85132cabda304eb4e00259d3`：entry `0xfffffff0073ec488`字`1400001e`跳blue；MMU hook `0xfffffff0073ee350`字`17fff884`跳magenta；post-MOV `0xfffffff0073ee370`字`d2a00600`未改变。四条MOVK覆盖64位所有半字，x0/x1均完整设为`0x3454593d`，M/C/I为1；不是子代理误读的`0x593d3454`。这些静态事实仍不证明MMU实际开启成功。

## 日志环 v2 只读验收（摘要）

`read-kmsg-ring.py` v2 的一次只读实跑，命令与结果如下（原命令）：

```
python3 scripts/read-kmsg-ring.py --pull --serial d25f844e \
  --expected-bytes 16777216 --timeout 30 --json
```

- 退出码 0；读取长度精确为 16777216 字节，SHA256 `2ff60d21bc18e94469a659ddc5e3ed8749c6791450ec11669b46a76044d0c7ef`。
- 槽位状态：7 live / 1 erased / 0 malformed；missing 与 unknown 计数为 0；索引 834..840。
- 整槽读取结果与 `--file` 读取同一份 raw 的结果一致，说明切槽与解析走的是同一布局。
- 各槽 `Linux version` 字段全部为 `null`，如实记录，没有从缺失字段推断任何内容。
- 结论字段：`phase_verified=false`，`origin=recovery_linux_or_unknown`。也就是说，这些是 Linux/恢复侧的痕迹，不构成 XNU 阶段证据。
- 前后只读核对：boot 与 recovery 分区 SHA256 均仍为 `8f488a0a2a766f0a1076a21be0a9f0187b2ee8e598bebd64391236b6d155fb1b`；本次没有重启、没有写入。
- 私有 artifact（**不上传**）：raw `/tmp/gauguin-kmsg-ring/ring-20261028T160931Z-f45bc078/sda15-ring.bin`，报告的本地副本为 `/tmp/gauguin-ring-acceptance-2c8685ff-c4b8-4530-ad23-d83c98e482de.json`。

修复史（保留，避免重复踩坑）：早期一次 `dd` 输出把 stderr 合并进 Stdout，导致长度多出 84 字节；改为 `dd ... 2>/dev/null` 后长度精确。`--expected-bytes` 的长度 gate 保留，且不截断。

术语更正：不要把 gzip 尾部里的 FDT 叫作 payload 的 ADT。gzip tail 是 **Linux FDT**，payload 的 **Apple ADT** 是另一个 12468 字节的文件（哈希见 `fat-recovery-verification.json`），两者不可混称。

## 本仓库发布什么、不发布什么

本文件与两个 JSON 只发布**摘要、哈希和一行结论**。以下原始材料保存在主机上，**不提交**到本仓库：

- TWRP 的原始 `dmesg`、`/tmp/recovery.log`、`/proc/cmdline`、pstore 目录清单与文件内容；
- `sda15` kmsg 环的 raw 读取结果；
- 各类 `boot.img` 与 128 MiB TWRP 备份镜像；
- auto-debug 运行目录及其二进制 stdout/stderr（`/tmp/gauguin-verified-auto-debug/...` 仅作本地引用，在 `auto-debug-verification.json` 中标注为 not uploaded）。

设备 serial 是脚本的默认作用域，按此处理即可，不额外夸大其私密性。`fat-recovery-verification.json` 中的路径已改为相对本仓库的 `../UniXNU/...`、`../uefi-gauguin/...`、`../boot_backup.img`，不再记录主机绝对路径；哈希与 `artifact_scope` 保持不变。

## 下一步

主机闭环已实现并实测。后续研究MMU后最小探针；所有物理MMIO访问必须与CPU映射阶段匹配，不重用不安全SMMU原型。现有standalone stage3使用PSCI SYSTEM_RESET并在返回后无限WFE，但固定基线payload没有该reset探针；启用前必须校准复位调用和恢复回退，不能宣称总能自动恢复。delay-only探针不提供独立信号，不用回归时长证明阶段。
