# ThorFAN 开发笔记

写给想读懂或修改这份代码的人，也写给几周后忘记细节的作者本人。

这里记录的是**为什么代码是这样写的**：已验证的硬件事实、踩过的坑、以及每个不
显然的设计决定背后的理由。README 讲怎么用，这份文档讲怎么改。

英文读者请注意：这份文档目前只有中文版，但代码注释、提交信息和 README 都有
英文。欢迎提 PR 翻译。

最后更新：2026-09-22（v0.1.0 首次开源）
开发与验证硬件：NVIDIA Jetson AGX Thor Developer Kit，L4T R38.4.0，
Ubuntu 24.04，Python 3.12.3

---

## 1. 这个项目要解决什么

原厂 `nvfancontrol` 的风扇曲线把转速锁在 **5371 RPM**，而风扇实测能跑到 **13658 RPM**。结果是跑大模型推理时 SoC 稳定在 108~109 °C，GPU 已经在降频，风扇却只用了约 40% 的能力。

实测对比（27B 模型持续推理负载下）：

| 状态 | PWM | 转速 | tj 温度 |
|---|---|---|---|
| 原厂闭环 | 97 | 5400 RPM | 108.9 °C |
| 手动满速 | 255 | 13658 RPM | 103.1 °C |

ThorFAN 的目标是把这部分余量放出来，同时提供图形界面和安全兜底。

一个重要的预期管理：**满速只换来 5.8 °C**。这说明散热器和风道已接近饱和，光靠风扇解决不了根本问题。如果目标是彻底脱离降频区间，必须配合 `nvpmodel` 降功耗档或收敛推理参数（当时的负载是 `--ctx-size 262144` 的 27B 模型，KV cache 是主要热源之一）。这一点最好在 README 里对用户讲清楚，别让人以为装了工具就能降 20 度。

---

## 2. 代码结构

```
thorfan/core/hwmon.py     sysfs 硬件抽象层（温度、转速、功耗轨、降频告警）
thorfan/core/policy.py    曲线插值 + 安全联锁
thorfan/core/vendor.py    nvfancontrol 协作与 profile 解析
thorfan/core/config.py    配置持久化
thorfan/core/control.py   运行时控制 socket（CLI/TUI <-> daemon）
thorfan/core/daemon.py    控制循环 + 所有权管理 + 请求处理
thorfan/cli.py            命令行入口
thorfan/tui.py            curses 实时界面 + braille 曲线
thorfan/gui/              空包，GTK4 界面的位置
packaging/thorfan.service systemd 单元
tests/                    138 项测试，全部通过，不需要硬件
```

跑测试：

```bash
python3 -m pytest tests/ -q
```

CLI 子命令：

```bash
python3 -m thorfan.cli status       # 温度、转速、功耗、降频状态（免 root）
python3 -m thorfan.cli profile      # 原厂曲线（免 root）
sudo python3 -m thorfan.cli tui     # 实时界面 + 交互调速（自带控制循环）
sudo python3 -m thorfan.cli set 90% # 立即改速，也接受裸 pwm 值 0-255
sudo python3 -m thorfan.cli mode curve   # vendor / curve / manual
sudo python3 -m thorfan.cli daemon  # 只跑控制循环（systemd 用这个）
sudo python3 -m thorfan.cli restore # 交回 nvfancontrol
```

`tui`、`set`、`mode` 在没有 daemon 时会**自己起一个控制循环**（见第 4 节），
所以不需要先开 daemon。`--save` 则只写配置给 systemd 服务用，不起循环。

`set` 和 `mode` 默认**不持久化**，加 `--save` 才写配置文件。没有 daemon 在跑时
两者退回「存配置，下次启动生效」。

2026-09-20 复验输出（空载，nvfancontrol 在管）：

```
fan     pwm=58/255 enabled=True rpm=1773
vendor  nvfancontrol=active
thorfan daemon not running

tj-thermal        59.8C   114.5C   54.7C
gpu-thermal   unavailable  114.5C     n/a    <- 见 3.5
```

注意这台机器实际的原厂 profile 比第 3.2 节的示例多几行（24/29/35 三档），
最低档同样是 `77 / 1750`，最高档同样是 `255 / 5371`，结论不变。

### 未完成

- **GUI 完全没写**。`thorfan/gui/` 只有一个空包（`__init__.py` 里写了它需要
  什么）。控制 socket 和 `telemetry` 接口已就绪，`thorfan/tui.py` 可作布局参考。
  注意 `pyproject.toml` 里**故意没有** `thorfan-gui` 入口点——一个 import 就失败
  的命令比没有这个命令更糟。
- **曲线编辑**。曲线模式读 `/etc/thorfan/config.json`，只能手编。socket 协议
  预留了扩展空间，加一个 `{"action": "set", "curve": [...]}` 分支即可。
- **原厂 profile 编辑**。直接改 `/etc/nvfancontrol.conf` 把 `5371` 提到 `9500`
  是比常驻 daemon 更轻量的方案，`vendor.backup_vendor_conf()` 已实现备份。
- **`data/` 仍是空的**（预留给 GUI 的图标、.desktop、GResource）。

### 未在真机验证的路径

这几条有单元测试覆盖但没在硬件上跑过，改动相关代码时要格外小心：

- **systemd 单元**。`systemd-analyze verify` 通过，但没装过。最关键的是
  `kill -9` 之后 `ExecStopPost` 能不能真的把风扇交回去——那是 `FanOwnership`
  覆盖不到的唯一路径。
- **`thorfan` group 免 sudo 调速**。逻辑有测试（含 group 不存在时的降级），
  但没在真机建 group 验证过。注意它只在已有 root daemon 时有用，非特权进程
  起不了控制循环。
- **`sudo thorfan tui` 的嵌入式控制循环**。非 root 降级为只读已验证。

### 已在真机验证

`daemon` / `set` / `mode` 的完整路径跑通了，日志确认所有权转移正确：

```
INFO  starting in vendor mode, polling every 2.0s, 5 thermal zones
INFO  accepting runtime commands on /run/thorfan.sock
INFO  stopping nvfancontrol to take over the fan     <- set 90% 触发接管
INFO  runtime change: mode=manual pwm=230
INFO  runtime change: mode=manual pwm=76
INFO  restoring nvfancontrol                          <- mode vendor 触发释放
INFO  runtime change: mode=vendor pwm=unchanged
```

`systemctl is-active nvfancontrol` 在最后返回 active，所有权确实交回去了。
pwm 230 实测 13572 rpm，pwm 76 实测约 4000 rpm。

这次验证暴露了三个 bug，都已修（见第 4 节）：配置写进 `/root/.config`、
立即报告的 rpm 是旧值、vendor 模式谎报 pwm 是自己设的。**在真机上跑一遍就能
发现三个单元测试发现不了的问题**，这是为什么第 5 节坚持要求硬件验证。



---

## 3. 硬件事实（花了力气才搞清楚的）

这一节是本文档最有价值的部分。以下每条都是实测验证过的，不要凭直觉推翻。

### 3.1 `pwm1_enable=0` 是关闭风扇，不是「手动模式」

这是最大的坑。直觉上 `_enable=0` 应该表示「脱离自动控制，转为手动」，实际上它
**直接关掉风扇输出级**。在 `enable=0` 状态下写 `pwm1` 完全无效。

调试时踩过一次：在 109 °C 的机器上执行了 `echo 0 > pwm1_enable`，风扇从 5300 RPM
掉到 1600 RPM（靠气流惰转），而当时温度距临界点只剩 5.5 °C。

正确的手动控速顺序是**先写 pwm1，再写 pwm1_enable=1**：

```bash
sudo systemctl stop nvfancontrol
sudo sh -c 'echo 255 > /sys/class/hwmon/hwmon1/pwm1; echo 1 > /sys/class/hwmon/hwmon1/pwm1_enable'
```

`hwmon.py` 的 `set_pwm()` 已经按这个顺序实现。

### 3.2 原厂 profile 的第一列不是温度

`/etc/nvfancontrol.conf` 里 `FAN_PROFILE` 表的第一列在 `TMARGIN ENABLED` 时
表示**距离临界温度的余量**，不是绝对温度。**数值越小越热。**

```
#TEMP   HYST    PWM     RPM
0       0       255     5371    <- 余量 0，即最热时
15      0       255     5371
24      0       192     4170
45      0       77      1750    <- 余量 45，即最凉时
115     0       77      1750
```

所以 `0 0 255 5371` 这行是在 SoC 顶到上限时生效的，不是冷机时。手改这个文件的人
十有八九会在这里搞反。

我们自己的 `FanCurve` 故意**用绝对温度**，因为那才是用户能直观理解的量。

### 3.3 `close_loop` 模式下 PWM 列只是参考

配置里 `FAN_CONTROL close_loop` 时，nvfancontrol 追的是 **RPM 列**，PWM 列基本被
忽略。这解释了一个初看矛盾的现象：表里写 `PWM 255`，实际 `pwm1` 却只有 97——
因为闭环打到 5371 RPM 目标就收手了，而 5371 只是风扇能力的 40%。

`RPM_TOLERANCE 100` 意味着 5335~5450 都算达标。

### 3.4 Tegra 温度传感器会间歇返回 EAGAIN

读 `/sys/class/thermal/thermal_zone*/temp` 偶发失败，`errno 11`
（`EAGAIN`/`Resource temporarily unavailable`），负载下更容易触发。

更阴险的是 Python 的缓冲文本 IO 会把它伪装成毫不相关的错误：

```
TypeError: can't concat NoneType to bytes
```

看到这个 TypeError 不要去查编码问题，它就是 EAGAIN。`hwmon.py` 里改用
`os.open` + `os.read` 并加了 5 次退避重试，压测 1000 次读取零错误。

### 3.5 `gpu-thermal` 在 GPU 空闲时完全读不出来

这是 3.4 之外的**另一个问题**，一开始没发现，因为当时 GPU 在跑推理。

GPU 被 railgate（断电门控）时，`thermal_zone1/temp` **每一次**读取都返回
EAGAIN。实测 300 次读取 300 次失败，不是偶发，重试策略完全无效：

```bash
$ cat /sys/class/thermal/thermal_zone1/temp
cat: .../temp: Resource temporarily unavailable
```

GPU 一旦有负载就恢复正常。这个坑的危险性在于：`hottest()` 原来直接读所有 zone，
只要 GPU 空闲就抛 `HardwareError`，daemon 每个 poll 周期都会异常。空载启动
daemon 是最常见的场景，所以这是必踩的。

处理方式（`ThermalZone.try_temp_c()` + `FanController.hottest()`）：

- 读不出来的 zone 直接跳过，用剩下的 zone 判断
- **所有** zone 都读不出来时，非 vendor 模式强制满速（没有温度就只能假设最坏），
  vendor 模式抛异常（nvfancontrol 有自己的保护，我们不该插手）
- `thorfan status` 把这种 zone 显示成 `unavailable` 而不是隐藏，否则看起来像
  传感器消失了

对应测试：`test_unreadable_zone_is_skipped`、
`test_all_zones_unreadable_forces_full_speed`、
`test_all_zones_unreadable_raises_in_vendor_mode`、
`test_zone_recovering_resumes_normal_control`。

注意 tj-thermal 通常比 gpu-thermal 高或接近，所以跳过 GPU zone 在实践中不会
低估温度。但这是经验观察，不是保证。

### 3.6 hwmon 编号不保证稳定

开发机上恰好是：

| 节点 | name | 用途 |
|---|---|---|
| hwmon1 | `pwmfan` | `pwm1`、`pwm1_enable` |
| hwmon4 | `pwm_tach` | `rpm`（只读） |
| hwmon3 | `tmp451` | 板载温度 |
| hwmon5 | `ina3221` | 功耗 |
| hwmon0 | `nvme` | 硬盘温度 |

但编号跨重启可能变。`hwmon.py` 按 `name` 文件内容查找设备，不要退回硬编码索引。

同理 `/sys/class/thermal/cooling_device*` 的编号也别靠 `ls` 顺序猜——`ls` 是字典序，
`cooling_device10` 会排在 `cooling_device2` 前面。我第一次就是这样把
`pwm-fan`（实际 device7）误认成 device11 的。

### 3.7 `/etc/nvfancontrol.conf` 不属于任何 deb 包

```bash
dpkg -S /etc/nvfancontrol.conf   # no path found
```

`nvidia-l4t-nvfancontrol` 包只提供 `/etc/nvpower/nvfancontrol/` 下的模板
（按板型命名，如 `nvfancontrol_p3701_0000.conf`），`nvpower` 在启动时按板卡 ID
复制出活动配置。

含义：改这个文件**不会被包升级覆盖**，但重新刷机或 nvpower 重新初始化会重置。
备份要自己留。`vendor.py` 的 `backup_vendor_conf()` 会存到
`/etc/nvfancontrol.conf.thorfan-backup`。

**注意：到目前为止这台机器上的 `/etc/nvfancontrol.conf` 从未被修改过**，仍是原厂状态。

### 3.8 其他环境事实

- 温度临界点：tj / gpu / cpu / soc012 / soc345 全部 **114.5 °C**
- GPU 在约 109 °C 开始降频（`gpu-throttle-alert` cur=1，GPC 掉到 18/169 档）
- 功耗模式当时是 **MAXN**（无上限），`nvpmodel -q` 可查
- 实测 PWM→RPM：77→3630，97→5400，255→13658（大致线性）
- 服务名 `nvfancontrol.service`，在 `/etc/systemd/system/`，enabled

---

### 3.9 转速表滞后于 PWM 变化好几秒

改完 pwm 立刻读 `rpm`，拿到的是**变化前**的转速。风扇有惯性，转速表也有采样
窗口。实测（`set 90%` 紧接 `set 30%`）：

```
set 90% -> 报告 1785 rpm    <- 这是 30% 之前的旧值（其实是原厂 58 的值）
set 30% -> 报告 13572 rpm   <- 这是刚才 90% 的值
```

两次都反了，看起来像设置起了反作用。第一版 CLI 就是这样把 rpm 当作「结果」
打印出来的，非常误导。现在改成只打印温度，并提示「几秒后跑 status 看稳定
转速」。GUI 做实时曲线图时同样要注意：pwm 是立即的，rpm 不是。

顺带确认了 PWM→RPM 的换算：pwm 76（30%）约 4000 rpm，pwm 230（90%）约
13572 rpm，与第 3.8 节的 255→13658 一致。

### 3.10 除温度外还能读到什么

调研 TUI 时把这台板子所有可读节点过了一遍，结论如下。注意 **hwmon 编号确实变
过**：`pwm_tach` 从 hwmon4 变成了 hwmon2，正好印证 3.6 节。

**功耗（`ina3221`）** — 最有价值的补充，因为它解释了为什么温度降不下来。
三条带 label 的轨：

```
VDD_GPU           12040 mV x 640 mA
VDD_CPU_SOC_MSS   12040 mV x 960 mA
VIN_SYS_5V0        5088 mV x 1740 mA
```

瓦数 = `inN_input` x `currN_input` / 1e6。空载约 22 W，轻载约 40 W。

坑：`in4`~`in7` 也有 `inN_input`，但它们是分流电压和求和（`in7_label` =
"sum of shunt voltages"），不是独立电源轨。把它们算进总功耗会重复计数。
`discover_rails()` 因此要求同时存在 `inN_label` 和 `currN_input`，并排除
label 含 "shunt" 的。

**降频告警（`cooling_device*`）** — 比温度更直接的「是否在降频」信号：

```
cpu-throttle-alert     cur=0/2
gpu-throttle-alert     cur=0/2    <- 109 C 时变 1
soc012-throttle-alert  cur=0/2
soc345-throttle-alert  cur=0/2
devfreq-gpu-gpc-0      cur=0/169  <- GPU 频率档位
devfreq-gpu-nvd-0      cur=0/182
cpufreq-cpu0..12       cur=0/48
```

`discover_throttles()` 排除了 `pwm-fan`（那是风扇控制的输出，不是症状），
并按「告警优先、各自字母序」排序而不是按路径——因为 `cooling_device10` 字典序
排在 `cooling_device2` 前面，这正是当初把 `pwm-fan`（device7）误认成 device11
的原因。

**其他**（暂未使用）：`tmp451` 两路板载温度（比 SoC 低约 5 C）、`nvme`
硬盘温度、`soctherm_oc` 过流事件计数（正常恒为 0，非 0 说明供电出过问题）。

### 3.11 TUI 选 curses 而不是 rich

系统里其实装了 `rich` 13.7.1（Ubuntu 的 `python3-rich` 包）和 `urwid`，
`textual` 没有。还是选了标准库 `curses`，理由：核心代码零第三方依赖，而实时
界面是核心功能不是可选功能；这个工具的用户群（折腾 Jetson 的人）恰好会介意
多装东西。

代价是条形图要自己画，但这反而是好事——自己画才能精确控制，比如用不同颜色在
转速条上标出原厂 5371 RPM 上限那条竖线（`┃`）。一张表格里的数字无法传达
「原厂只用了风扇 40% 的能力」这件事，一条画在位置上的线可以。

开发时的 shell 是 `TERM=dumb`，`curses` 需要真终端。用 `script -qc` 套一层
伪终端可以做冒烟测试：

```bash
timeout 6 script -qc "TERM=xterm-256color python3 -m thorfan.cli tui" /dev/null \
  < <(sleep 2; printf 'q')
```

单元测试不测渲染本身，测的是「画什么」的决策：条形图缩放、颜色阈值、按键处理、
无 daemon 时的降级。渲染用 `FakeWindow` 跑一遍，专门抓越界崩溃（窄终端那个
case）。

### 3.12 传感器重试的退避是显示路径上的主要开销

加曲线前先量了一下 `Reader.snapshot()`，**38.9 ms**，只能跑 26 Hz。逐项拆开：

```
zones temp           31.83 ms   <- 全部开销在这里
vendor.is_active      6.17 ms
rails watts           1.63 ms
throttles cur_state   0.16 ms
fan pwm+rpm           0.02 ms
```

但单独测每个 zone 的读取都只要 **0.07 ms**。差异来自 3.5 节那个 power-gated 的
`gpu-thermal`：它每次读都失败，于是 `_read()` 的 5 次重试全部走完，退避累计
`2+4+6+8+10 = 30 ms`，**纯粹在等一个永远不会成功的读取**。

修法是给 `try_temp_c()` 换成不重试的 `_read_once()`。重试阶梯是为了应对 3.4 节
那种「采样在途」的瞬时 EAGAIN，而对 gated zone 每次都失败。显示路径宁可丢一个
采样点（下一帧就补上了），也不该阻塞 30 ms。控制循环仍然用带重试的 `_read()`，
因为它 2 秒才跑一次，且做的是安全决策。

`vendor.is_active()` 那 6 ms 是 `systemctl` 子进程开销。daemon 的 telemetry 改成
直接看 `FanOwnership.owned`（本地已知，不必问 systemd），只读路径缓存 5 秒。

结果 **38.9 ms → 2.6 ms**，15 倍。曲线渲染本身（两条线、600 采样点、100 列）
只要 0.29 ms，可以忽略。

教训：性能问题的根因往往不在「看起来慢」的那段代码里。读 sysfs 本身极快，慢的
是为容错加的重试在一个「永久失败」的场景下退化成了固定延迟。

### 3.13 用 braille 画曲线

曲线用 braille 点阵（U+2800 区块），每个字符 2 列 x 4 行子像素，所以 6 行终端
能画出 24 像素高的图。位布局不直观，记在这里：

```
从 U+2800 起的位偏移
 1   8      左列自上而下: 1 2 4 64
 2  16      右列自上而下: 8 16 32 128
 4  32
64 128
```

三个设计决定：

**缺失采样留空白，不插值**。power-gated 的 zone 会产生真实的数据缺口，用直线
连过去是在编造数据。

**历史不足时右对齐，不拉伸**。刚启动只有 3 个采样点时，把它们铺满整个宽度会让
时间轴说谎。右对齐意味着曲线从左边长出来。

**温度和占空比共用纵轴**，各按自己的量程缩放（温度 30~115 °C，占空比 0~100%）。
单位不同，严格说不该叠在一起，但用户真正要看的是**响应形状**——风扇对温度上升
的反应快不快、有没有过冲。为此两条线必须画在一起。温度线画在占空比之上，因为
温度是主角。

窄终端里曲线是第一个被牺牲的元素（`height >= y + CHART_ROWS + 5` 才画），
实时读数必须始终可见。`g` 键可以手动开关。

## 4. 架构决策与理由

### 为什么 vendor 模式绝不写入

两个控制器同时写 `pwm1` 会互相打架产生振荡。`policy.py` 里 vendor 模式**只读不写**，
即使检测到过热也不写（nvfancontrol 有自己的保护）。有测试专门锁这个行为：
`test_vendor_mode_never_writes`。

要接管必须显式 `systemctl stop nvfancontrol`，由 `daemon.py` 的 `FanOwnership`
上下文管理器负责，保证任何退出路径（含异常和 SIGTERM）都恢复原厂守护进程。
如果原厂守护进程拉不起来，**兜底策略是强制满速**而不是保持最后设定值。

### 为什么安全阈值是 100 °C 带滞回

临界 114.5 °C，GPU 约 109 °C 降频，所以在 100 °C 就强制介入，留足余量。
滞回到 95 °C 才解除，避免在阈值附近反复切换。

这个设计的直接动因是调试时真的让风扇在 109 °C 停转过。一个能停风扇的 GUI
必须有硬保护，用户设 pwm=0 也要被推翻。见 `test_emergency_overrides_manual_stop`。

### 为什么损坏的配置降级而不报错

配置文件解析失败时退回 vendor 模式 + 默认曲线，而不是抛异常拒绝启动。理由是
手改坏了一个 JSON 不该导致风扇处于无人管理状态。见 `test_corrupt_config_yields_defaults`。

### 配置文件路径：root 永远用系统路径

原来的 `default_config_path()` 是「系统配置存在就用它，否则用用户目录」。在
`sudo` 下这会写到 `/root/.config/thorfan/config.json`——**看起来能用**，因为
daemon 也是 root 会读到同一个文件，但这是巧合。一个环境干净的 daemon
（systemd 不传 `HOME`）或另一个特权调用方都读不到那里。

现在 root 无条件用 `/etc/thorfan/config.json`，非 root 保持原逻辑（系统配置
存在就读它，这样免 root 的 `status` 反映的是 daemon 实际在用的配置）。

真机上踩到过一次，`mode vendor --save` 输出了
`saved mode=vendor to /root/.config/thorfan/config.json`。那个残留文件可以删。
对应测试在 `tests/test_paths.py`。

### 为什么 vendor 模式的响应不报告 pwm

`mode vendor` 原来会打印 `applied mode=vendor pwm=76/255 (30%)`，读起来像是
ThorFAN 把风扇设成了 30%。实际上 vendor 模式下我们根本不写 pwm1，那个 76 只是
nvfancontrol 刚接手瞬间的 sysfs 读数。

现在 `_status_payload()` 在 vendor 模式下额外带一个 `pwm_is_ours: false`，
CLI 据此改成只说「nvfancontrol 又在驱动风扇了」。GUI 也应该尊重这个标志，
不要把 vendor 模式的 pwm 显示在「当前设定」那一栏。

### 为什么 TUI 和 `set` 会自己起控制循环

第一版要求用户先在另一个终端跑 `sudo thorfan daemon`，再开 TUI 或 `set`。这是
接口缺陷：用户想调风扇，不想管进程拓扑。

但也不能让 CLI 直接写 `pwm1`（见下一节）。所以改成「需要控制循环时就起一个」：

- `thorfan tui` 以 root 运行且没有现存 daemon 时，在后台线程里起一个
  `Daemon`，退出时 `request_stop()`，走正常的 `FanOwnership.release()` 交回
  nvfancontrol。`sudo thorfan tui` 于是是一条自足的命令。
- `thorfan set 90%` / `mode curve` 没有 daemon 时，在**前台**起一个并带着设定
  值启动，Ctrl-C 退出时交回风扇。前台而不是后台，因为一个能停风扇的循环不该
  在用户看不见的地方运行。
- `--save` 保留原行为（只写配置给 systemd 服务用），因为那才是「记下来，以后
  生效」的语义。
- `mode vendor` 不起循环，直接 `vendor.start()`——vendor 模式本来就是板子自己
  的行为，为它开一个什么都不做的循环没有意义。

**已有 daemon 时绝不取代它**，只连过去。两个控制循环同时写 `pwm1` 会振荡。

三个实现细节：

`Daemon.run()` 加了 `install_signal_handlers` 参数，因为 `signal.signal()` 只能
在主线程调用，嵌入式运行时必须关掉，由宿主调 `request_stop()`。

嵌入式运行时要**静音日志**。daemon 的 log 行会直接画到 curses 布局上，所以加
`NullHandler` 并关掉 propagate。

socket 绑定失败**不再是致命错误**。原来 `ControlServer` 用 `with` 包住循环，
开不了 socket 就整个 daemon 起不来——但那时风扇已经被接管了，拒绝启动等于把风扇
留在无人管理状态。现在绑定失败只记警告，控制循环照常跑（只是外部客户端连不上）。
对应测试 `test_daemon_runs_without_a_control_socket`。

### 为什么运行时改速走 socket 而不是直接写 pwm1

`thorfan set 90%` 要「立即生效」，最省事的实现是 CLI 直接写 `pwm1`。**不要这样
做。** 安全联锁（100 °C 强制满速、滞回到 95 °C）活在 daemon 的控制循环里，
一个写完 pwm1 就退出的短命进程留下的是一个没有任何联锁的固定转速风扇——正是
这个项目要避免的状态。

所以改成：daemon 起一个 Unix socket（`/run/thorfan.sock`，0600 root-only），
CLI 把请求发给它，由 daemon 在自己的循环里应用。协议是一行一个 JSON 对象，
请求—响应。

几个设计点：

- **改动同步应用**。`_handle_set` 在持锁状态下直接调 `_apply_now()` 跑一次
  `controller.step()`，所以命令返回时风扇已经变了，不用等下一个 poll 周期。
  正因为是同步的，控制循环不需要被唤醒，`_wake` 只用于停止。
- **模式切换要同时转移所有权**。切到 vendor 模式必须 `release()`（启动
  nvfancontrol），切到 curve/manual 必须 `acquire()`（停止它）。漏掉任一边都会
  变成两个控制器抢 `pwm1`。`FanOwnership` 因此加了幂等的 `acquire`/`release`。
- **单例保护**。两个 daemon 同时写 pwm1 会互相打架产生振荡，所以
  `ControlServer.start()` 先 `probe()` 一下，有活的 daemon 就抛
  `AlreadyRunning` 拒绝启动。被 `kill -9` 留下的 stale socket 文件会被识别并
  清掉（probe 连不上就 unlink），不会永久阻塞启动。
- **socket 开在 ownership 块内部**。这样它的生命周期不会超出「我们有能力真正
  驱动风扇」的时间窗。
- **请求处理异常不能打死通道**。`_handle_connection` 捕获所有异常转成错误响应，
  因为这个通道正在维持风扇有人管理的状态。有测试锁这个行为
  （`test_handler_exception_does_not_kill_the_server`）。
- **没有 daemon 时降级为写配置**。`_apply_or_persist` 捕获
  `DaemonNotRunning`，退回原来的「存配置，下次启动生效」行为，并明确告诉用户。

`restore` 也改了：先尝试让运行中的 daemon 切到 vendor 模式，而不是直接
`systemctl start nvfancontrol`。否则 daemon 还在写 pwm1，刚拉起来的
nvfancontrol 会立刻和它打架。

权限上 socket 默认 root-only，但如果系统里存在 `thorfan` group，绑定时会
`chown` 到该 group 并放宽到 0660，让 TUI/GUI 能免 sudo 调速：

```bash
sudo groupadd -f thorfan
sudo usermod -aG thorfan "$USER"
sudo systemctl restart thorfan    # socket 在 bind 时才读 group
```

放宽是 opt-in 的（group 不存在就保持 0600），因为能连上 socket 就等于能驱动
风扇。group 不存在或 chown 失败都只记日志不报错——为了一个权限问题拒绝启动
daemon，结果是风扇无人管理，那更糟。

一个副作用：非 root 且不在 group 里的 `thorfan status` 连不上 socket，只能通过
「socket 文件存在」推断 daemon 在跑，看不到它的模式。现在会输出
`thorfan daemon running (re-run as root to see its mode)`，而不是把权限错误
当成故障报出来。

`telemetry` action 是给 TUI 加的：一次往返返回所有 zone、功耗轨、降频状态和
风扇读数。与 `status` 的区别是它**现场读传感器**而不是复用上一次控制循环的
结果，因为界面刷新比 poll 间隔快。读失败的传感器返回 `null` 而不是省略字段，
这样界面能显示 "unavailable" 而不是静默少一行。

JSON 细节：`max_temp_c` 在所有传感器都读不出来时是 `nan`，而 `json.dumps` 会
输出裸 `NaN`——Python 能解析，但那不是合法 JSON，别的语言写的客户端会炸。
现在序列化成 `null`。

### 为什么 systemd 单元不写 `Conflicts=nvfancontrol.service`

这是写 `packaging/thorfan.service` 时最值得记下来的一个决定。

直觉上 `Conflicts=nvfancontrol.service` 正好表达「两者互斥」，但它会和
`FanOwnership` 打架，形成一个环：

1. 停止 thorfan.service，daemon 在退出时自己 `systemctl start nvfancontrol`
2. systemd 看到 nvfancontrol 起来了，按 Conflicts 去停 thorfan.service
3. thorfan.service 本来就在停，于是 nvfancontrol 又被再启一次

风扇所有权必须只有一个权威。既然 `FanOwnership` 已经保证了所有退出路径
（含异常和 SIGTERM）都恢复原厂守护进程，就让它独占这个职责，systemd 只用
`After=nvfancontrol.service` 排个启动顺序，让原厂守护进程先稳定下来。

`ExecStopPost=/usr/bin/thorfan restore` 是第二道保险，覆盖 `FanOwnership`
覆盖不到的情况：SIGKILL 和硬崩溃。`restore` 是幂等的（`systemctl start` 一个
已 active 的服务是空操作），所以正常退出时多跑一次也无害。

`Restart=on-failure` + `StartLimitBurst=5` 的组合是刻意的：风扇不能长期无人
管理，所以要自动重启；但反复失败的 daemon 应该彻底停下而不是空转，到达上限
时 systemd 停掉单元，而此时 `ExecStopPost` 已经把风扇交回原厂了。

注意 `StartLimitIntervalSec`/`StartLimitBurst` 属于 `[Unit]` 段而不是
`[Service]`（systemd 229 之后改过位置），写错位置不会报错，只是静默失效。

`ProtectSystem=strict` 没有启用，因为 daemon 需要写 `/sys` 下的 `pwm1` 和
`/etc/thorfan`，要用的话必须显式列 `ReadWritePaths`，收益不大。

### 建议的 GUI 架构

**不要把整个 GUI 跑在 root 下。** 推荐 daemon（root，实际调速）+ GUI（普通用户）
分离。控制 socket 已经就绪，GUI 直接用 `control.send()` 即可，不要自己碰 sysfs；
权限问题用 `thorfan` group 解决（见上），`pkexec` 也已确认可用
（`/usr/bin/pkexec`）。

GUI 技术栈：**GTK4 + libadwaita**，已验证可用（PyGObject 3.48.2）。
PyQt6 和 PyQt5 都没装，不要用。

```bash
python3 -c "import gi; gi.require_version('Adw','1'); from gi.repository import Adw; print('OK')"
```

注意开发时的 shell 是 `session=tty`（SSH/终端），GUI 必须在桌面会话里才能测。

`thorfan/tui.py` 可以当作参考：它已经解决了「显示什么、怎么配色、按键怎么映射」
这些问题，GUI 照搬布局逻辑即可。

---

## 5. 下一步

按优先级：

1. **装一次 systemd 单元跑通**。`daemon`/`set`/`mode` 的手动路径已验证，剩下的
   未知项是单元本身。注意 Ubuntu 24.04 有 PEP 668 保护
   （`/usr/lib/python3.12/EXTERNALLY-MANAGED`），`pip install -e .` 会被拒，要用
   `sudo pip install --break-system-packages .` 或 pipx。单元里写的是
   `/usr/local/bin/thorfan`（Debian/Ubuntu 上 `sudo pip install` 的落点），
   用 pipx 的话在 `~/.local/bin`，`ExecStart` 和 `ExecStopPost` 两处都要改——
   systemd 不搜索 PATH。装完检查 `systemctl status thorfan` 和
   `journalctl -u thorfan -f`，重点测两件事：`systemctl stop thorfan` 后
   nvfancontrol 是否恢复；`kill -9` daemon 之后 `ExecStopPost` 是否真的把风扇
   交回去了（这是手动前台跑覆盖不到的路径）。
2. **写 GTK4 界面**：实时温度/转速曲线图、可拖拽的风扇曲线编辑器、模式切换、
   原厂 profile 查看与编辑。控制通道已经就绪，GUI 直接用
   `control.send({"action": "set", "pwm": n})` 和 `{"action": "telemetry"}` 即可。
   相关资源文件放 `data/`（图标、.desktop、GResource），目录已预留。
   `thorfan/tui.py` 已经解决了显示什么、怎么配色、按键怎么映射，可以照搬。

   两个已知坑：**rpm 滞后于 pwm 好几秒**（见 3.9），实时图上两条线不会同步。
   **vendor 模式的 pwm 不是我们设的**，响应里有 `pwm_is_ours: false`，别显示
   在「当前设定」栏。
3. **TUI 还可以加的东西**：功耗曲线（History 加一条 series 即可，braille 渲染
   已经是通用函数）、曲线编辑（现在只能手编 JSON）、把 `nvpmodel` 档位显示出来
   （需要 root 跑 `nvpmodel -q`，或者读 `/etc/nvpmodel.conf` 自己解析）。
   注意刷新率现在是 1 Hz，加东西前先量一下 `Reader.snapshot()` 的开销
   （当前 2.6 ms），别把 3.12 节那个坑重新踩回去。
4. **曲线编辑**。`mode curve` 能切到曲线模式，但改曲线只能手编
   `/etc/thorfan/config.json`。socket 协议预留了扩展空间，加一个
   `{"action": "set", "curve": [...]}` 分支即可。
5. **考虑 profile 编辑功能**。如果用户不想常驻 daemon，直接改
   `/etc/nvfancontrol.conf` 把 `5371` 提到 `9500` 也是一种方案，改完
   `systemctl restart nvfancontrol` 即可。这个路径更轻量，值得作为一个选项。
   记得先备份（`vendor.backup_vendor_conf()` 已实现），且提醒用户第一列是余量
   不是温度。

### 许可证

**MIT**，`LICENSE` 文件已补，版权署名 `swsususu`，与 `pyproject.toml` 的
`license` 字段和 classifier 一致。

选 MIT 的理由：核心代码零第三方依赖（纯标准库），GUI 可选依赖 PyGObject 是
LGPL-2.1+，Python 的 import 属于动态链接不传染，所以没有任何协议约束。这个
项目的价值在于被人用、被人反馈平台细节，宽松许可传播阻力最小。

README 里额外加了一段免责声明。法律效力主要来自 MIT 的 "AS IS" 条款，但对
用户的实际警示作用来自 README——这个工具能在 108 °C 的板子上把风扇停掉。

### 改名说明

品牌名是 **ThorFAN**，但 Python 包名和仓库名用小写 `thorfan`（PEP 8 不允许包名
含大写）。这不是笔误，不要「修正」。

商标提醒：Thor 和 Jetson 都是 NVIDIA 商标。README 里写 "for NVIDIA Jetson AGX
Thor" 属于描述兼容设备，风险较低，但项目名含 Thor 仍有一定风险。开源前自行评估，
若想彻底规避可考虑 `mjolnir-fan` 之类。

另外 `jetson-fan-control`、`jetson-fan`、`jetson-fan-ctl` 这些名字都已被占用，
其中 `Pyrestone/jetson-fan-ctl` 有 479 stars，是该名字空间的事实占有者。
搜索时发现**没有任何针对 AGX Thor 的风扇工具**，也没人处理 nvfancontrol 的
close_loop/TMARGIN 配置，这是个真空区。

仓库地址：`git@github.com:swsususu/ThorFAN.git`。注意远端仓库名是大写
`ThorFAN`（品牌名），`git clone` 出来的目录也是 `ThorFAN`，而 Python 包名是
小写 `thorfan`。本地开发目录是 `~/thorfan`，两者不一致但无影响。

---

## 6. 恢复现场

如果调试中把风扇搞坏了，恢复原厂控制：

```bash
sudo systemctl stop thorfan      # 如果装了单元
sudo python3 -m thorfan.cli restore
```

`restore` 会先尝试让运行中的 daemon 切回 vendor 模式，联系不上才直接
`systemctl start nvfancontrol`。手工等价命令：

```bash
sudo systemctl start nvfancontrol
```

确认恢复（应看到 `enabled=True`，且 pwm/rpm 随温度变化）：

```bash
python3 -m thorfan.cli status
```

紧急情况强制满速（注意写入顺序）：

```bash
sudo systemctl stop nvfancontrol
sudo sh -c 'echo 255 > /sys/class/hwmon/hwmon1/pwm1; echo 1 > /sys/class/hwmon/hwmon1/pwm1_enable'
```

万一 `/etc/nvfancontrol.conf` 被改坏，原厂模板在
`/etc/nvpower/nvfancontrol/`，本机对应的应是
`nvfancontrol_p3834_*.conf` 系列之一（按 `/proc/device-tree/model` 匹配）。
