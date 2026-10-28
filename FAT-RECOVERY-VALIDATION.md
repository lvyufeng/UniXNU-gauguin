# FAT 修复与恢复验证交接

## 已验证

- 固件：`../uefi-gauguin/Platforms/Xiaomi/gauguinPkg/Drivers/ShengBootMemoryDxe/ShengBootMemoryDxe.c`。
- FAT 主机回归：在 `../uefi-gauguin` 执行 `python3 -m unittest discover -s tests`。最初一次验证为 21 项（historical，保留记录）；已发布代码经独立复核：**26 项 = 18 FAT + 8 DarwinBoot，0 skip、0 fail**。本仓库已提交的主机工具另计 **129 项**（53 auto-debug + 76 kmsg-ring）；post-MMU 探针 12 项与 install 探针 35 项属未提交的实验原型，不计入。
- 测试编译真实 C 代码，不是仅测试 Python 仿制算法。
- 真实 payload 的内核与 ADT 名字、大小、簇链、头和 SHA256 已分离验证。
- 修复点（长文件名，LFN）：旧算法把 `kernel.macho.adt` 和 `kernel.macho` 都解析为 `kernel.macho`，于是请求内核时读到了 ADT，Mach-O 解析拒绝该内容。现在按 UTF-16 码元取槽位 1..10、14..25、28..31 拼名，第 26 字节是保留字段、不作字符，并处理多段名字的序号与校验和以及 `0x00` 终止。该修复的首次主机验证为 21 项通过（historical）；发布代码的最终验证为上述 26 项（18 FAT + 8 DarwinBoot），见 `STAGE-COMMITS.md`。
- 当前 `payload.img` 是固定基线：72 MiB（75497472 字节），SHA256 `55c429b920b25057ffe9df615c042fa6886e326b5881d58cc5cf64173e850907`；其中内核 57110528 字节 SHA256 `6cc5894a917d0a93c117ace3338eda493b1a93cd85132cabda304eb4e00259d3`，ADT 12468 字节 SHA256 `65d42111e9f45588a6ac244ed66a28da35715072cfff699a07a10567f8768726`。
- `payload.json` 和独立 `kernel.macho` 与本次基线**不一致（stale）**；不可拿这些文件当 payload 内容的证据，也不可用它们反推真机行为。
- `build kernel` 不会自动替换 payload；UniXNU 已有显式 `build payload` 打包命令，但本次验证链刻意不执行它以保持基线。`bootimg` 默认使用已存在的 `payload.img`。测试新内核时必须明确打包、校验内容和哈希，再构建启动镜像，不能混用旧 manifest。
- 哈希与构建结果见 `fat-recovery-verification.json`；真机阶段证据与自动化记录见 `AUTOMATION-EVIDENCE.md` 和 `auto-debug-verification.json`。

## 真机已观察到的

- 用户明确看到面板出现 `DB13 parsed Success`。该检查点在成功读取内核并通过 `ParseKernel` 之后才出现，因此它证明：内核按其真实名字被读到、被完整解析；不证明段被映射到可用地址，也不证明 XNU 执行。
- 同一镜像上面板出现 IMG 1034 蓝色带（内核入口）与洋红（`SCTLR_EL1` 写入点之前），证明至少在内核入口到 `SCTLR_EL1` 之间执行过 XNU 代码。
- **不能**由此推断 MMU 之后、USB 枚举或 userland 运行；复位同样可能是 MMU 之后的故障。

## 恢复与回退现状

- boot 与 recovery 均为 134217728 字节，完整 SHA256 一致：`8f488a0a2a766f0a1076a21be0a9f0187b2ee8e598bebd64391236b6d155fb1b`，与本地 `../boot_backup.img` 一致（`recovery` 分区完整哈希也单独核对过）。
- 新固件始终以临时 `fastboot boot` 方式试验，未再次永久刷写 boot；boot 保持 TWRP 回退路径。boot 曾因早期一次误写受损，随后已恢复并完整核验（boot 与 recovery 均等于上文 128 MiB 备份哈希）。
- 历史临时试验镜像 SHA256 `249b581b1732411ad7564e6d0cf78d567f2b18b4463bfc02a24b7334c34d7994`：构建时用汇编 `.if 0` 禁用历史上向猜测 ramoops 地址写记录的代码。该 `.if 0` 已从源码删除并落地提交，所以它描述的是那个镜像的构建方式，不是当前源码状态。它仍是**唯一经过真机实测**的基线镜像。
- 新构建镜像 SHA256 `1e9180a10825efa70ef9fcb08a170a062a57dbe27fc530dec39541834b47f1a4`：**仅通过构建验收，未在真机启动**。payload 未变（`55c429b9…907`）。
- 主机闭环实测：`scripts/auto-debug.py` 默认 dry-run、不写设备、不刷写；加固后一次 `--run` 结果目录 `/tmp/gauguin-verified-auto-debug/20261028T143609.024127Z-0a9deb08c902`，pre boot ID `8aec1d8e-76ec-4ad8-83a9-7976bdb8e605`，恢复 boot ID `3ca04640-d01a-404b-8981-b399cad8755b`，`recovery_boot_verified` / `collection_verified` / `transport_verified` / `partition_hashes_unchanged` 均为 true，`phase_verified=false`，errors 为空，boot 命令完成到确认新 TWRP 85.3945 秒。该时长只说明运输与恢复，不证明任何固件阶段。
- 本轮离线回归：仓库 `python3 -m unittest discover -s tests` 运行 136 项、含 27 个子测试，全部通过。这是主机事实，且父代理会再次复核，不据此声称任何未来或真机结果。
- 已提交主机工具（`auto-debug.py` + `read-kmsg-ring.py`）的独立验收计数为 **129**（53 + 76）；`read-kmsg-ring.py` 的只读 v2 实跑摘要见 `AUTOMATION-EVIDENCE.md`。

## 当前缺少的证据

MMU 之后的任何阶段仍无证据：没有证明 MMU 已开启，没有证明 USB 控制器或 userland 运行。USB 不枚举不证明 XNU 正在执行，复位不证明任何 MMIO 操作成功。新安装探针尚未在真机验收，不能当作完成。

## 设备连接后的操作顺序

1. 用 `adb devices -l` 确认 serial `d25f844e` 正在 TWRP；之后每条命令指定 `adb -s d25f844e`。
2. 只读核对 `/dev/block/by-name/boot` 和 `recovery` 的链接与大小。预期两者 134217728 字节，不依赖旧的 sde55/sda29 编号。
3. 计算完整 boot 与 recovery 分区 SHA256，与 `8f488a0a...fb1b` 及本地 `../boot_backup.img` 比较。
4. 任何写回前：传送备份到 TWRP `/tmp`，再次核对传送后的 SHA256 与大小；目标必须是已核对的 by-name/boot。仅恢复 boot，不写 recovery、不清其他分区。
5. 写回后读整个 boot 分区 SHA256，必须等于备份；若任一步不匹配则停止写入并调查。
6. 若用主机自动化，优先 `python3 scripts/auto-debug.py --inspect --serial d25f844e`（只读）；需要一次临时启动时用 `--run` 并同时传 `--expected-sha256`（必要时加 `--expected-recovery-sha256`）。它只发一次 `fastboot boot`，绝不永久刷写。
7. 真机接受标准不变：请求 `kernel.macho` 得到 57110528 字节，头 `feedfacf 0100000c`，面板出现 `DB13 parsed Success`；然后才调查后续 handoff，不再将 loader 回菜单归咎于 XNU USB。

## 实验约束

- 不启用当前原型 SMMU bypass，不写共享 SMMU 控制寄存器。
- 不使用永久刷 boot 来试验新固件；boot 保持 TWRP 回退路径。
- 不更改或推送上游仓库；保留其他已有工作树修改。远程用户分支只有个人 `lvyufeng`，不是 UniXNU 上游，提交由父代理负责。
- 不用等待循环、USB VID 猜测、缺失的屏幕色带代替明确的阶段日志；也不用回归时长代替阶段证据。
- sda15 上的历史 Linux/TWRP kmsg 环可只读，但它不是 XNU 日志；pstore 为空不代表 DRAM 未被保留。只读 raw 环是允许的取证手段，不能把其中的 Linux 行当作 XNU 输出。

## DarwinBoot 诊断窗口

DarwinBoot 在解析成功后保留 15 秒诊断停顿，以便相机拍到 `DB13 parsed Success`。目前没有编译期开关；正常性能或时序测试前必须去除。