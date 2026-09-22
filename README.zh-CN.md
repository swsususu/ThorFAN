# ThorFAN

NVIDIA Jetson AGX Thor 的风扇控制与散热策略编辑器。

[English](README.md)

原厂 `nvfancontrol` 把风扇锁在 **5371 RPM**，即使 SoC 已经 109 °C、GPU 已经在
降频也不会再升。而风扇实测能跑到约 **13650 RPM**。ThorFAN 把这部分余量放出来，
并带安全联锁，避免误操作烤坏板子。

在 Jetson AGX Thor Developer Kit（L4T R38.4.0）上实测，负载为 27B 参数模型的
持续推理：

| 风扇状态 | 占空比 | 转速 | 结温 |
|---|---|---|---|
| 原厂闭环 | pwm 97 | 5400 RPM | 108.9 °C |
| 手动满速 | pwm 255 | 13658 RPM | 103.1 °C |

注意满速只换来 5.8 °C，参见[预期管理](#预期管理)。

## 状态

早期开发。控制层、命令行和终端界面可用；GTK4 图形界面尚未编写。

## 环境要求

- 带 PWM 风扇的 NVIDIA Jetson（在 AGX Thor / L4T R38+ 上开发）
- Python 3.10+，仅用标准库
- 任何驱动风扇的操作都需要 root
- GTK4 和 libadwaita 仅为未完成的 GUI 所需

## 快速开始

只看状态不需要安装。克隆后直接跑：

```bash
git clone https://github.com/swsususu/ThorFAN.git
cd ThorFAN
python3 -m thorfan.cli status
```

这条只读传感器，无需任何权限。要控制风扇，以 root 启动实时界面：

```bash
sudo python3 -m thorfan.cli tui
```

按 `q` 退出，退出时风扇自动交回 `nvfancontrol`。

这就是最小路径。以下内容是关于正式安装和其他命令的。

## 安装

Ubuntu 24.04 的 Python 标记为外部管理（PEP 668），直接 `pip install` 会被拒绝。
三种方案任选：

```bash
# A：不安装，始终在克隆目录里用 `python3 -m thorfan.cli`
# B：装到系统路径，systemd 单元默认按这个找命令
sudo pip install --break-system-packages .
# C：隔离安装，`thorfan` 会落在 ~/.local/bin
pipx install .
```

用 B 或 C 之后 `thorfan` 命令可直接调用，下文示例都用这种较短的写法。

### 开机自启

```bash
sudo install -m 644 packaging/thorfan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now thorfan
journalctl -u thorfan -f
```

单元里写的路径是 `/usr/bin/thorfan`。如果用 pipx 或虚拟环境，先把 `ExecStart`
和 `ExecStopPost` 改成实际路径。

单元**故意不写** `Conflicts=nvfancontrol.service`：ThorFAN 自己负责停止和重启
原厂守护进程，systemd 的互斥声明会和它打架。

### 免 sudo 控制

控制 socket 默认仅 root 可用。建一个 `thorfan` 组可以让普通用户通过界面调速：

```bash
sudo groupadd -f thorfan
sudo usermod -aG thorfan "$USER"
sudo systemctl restart thorfan   # socket 在绑定时才读取组信息
```

需要重新登录，或执行 `newgrp thorfan`，组成员身份才生效。

注意这只在**已有一个 root daemon 在跑**时有用，因为非特权进程无法启动控制循环。
常规搭配是 systemd 服务常驻 + 普通用户开界面。这个组的成员都能驱动风扇，授予
权限时按「等同于可以写 `pwm1`」来考虑。

## 使用

### 查看当前状态

```bash
thorfan status
```

```
fan     pwm=58/255 enabled=True rpm=1786
vendor  nvfancontrol=active
thorfan daemon not running

zone                  temp  critical   margin
tj-thermal           55.1C    114.5C    59.4C
soc012-thermal       55.1C    114.5C    59.4C
cpu-thermal          53.7C    114.5C    60.8C
soc345-thermal       53.7C    114.5C    60.8C
gpu-thermal       unavailable    114.5C      n/a

VDD_GPU                   0.00 W
VDD_CPU_SOC_MSS           6.51 W
VIN_SYS_5V0               6.52 W
total                    13.03 W

throttling  none
```

五路温度的含义：

| 传感器 | 含义 |
|---|---|
| `tj-thermal` | 结温。它是其他几路的**聚合最大值**，不是独立传感器。`nvfancontrol` 和降频判断都看这一路。 |
| `cpu-thermal` | CPU 集群 |
| `gpu-thermal` | GPU 核心。空闲时 GPU 被断电门控，显示 `unavailable`，这是正常的。 |
| `soc012-thermal` | SoC die 的一个区域 |
| `soc345-thermal` | SoC die 的另一个区域 |

五路共用 114.5 °C 临界点。因为 tj 是聚合值，它几乎总是最高的；看其他几路是为了
知道**热点在哪**。

`VDD_GPU` 显示 0.00 W 是同一个原因：GPU 断电时那条轨没有电流。

### 实时界面

```bash
sudo thorfan tui
```

```
ThorFAN  mode: manual
────────────────────────────────────────────────────
fan   pwm 230/255 ( 90%)  ████████████████████░░░░
      13572 rpm           ███████████┃░░░░░░░░░░░░  max 13650
                                     ┗ stock cap 5371
temps tj        *85.2 C   ██████████████░░░░░░░░░░  crit 114
      gpu        84.8 C   █████████████░░░░░░░░░░░  crit 114
      cpu        78.1 C   ███████████░░░░░░░░░░░░░  crit 114
power VDD_GPU            32.40 W
      VDD_CPU_SOC_MSS    14.22 W
      total              53.16 W
throt ● gpu  ○ cpu  ○ soc012
freq  gpu-gpc 18/169
chart ⠀⠀⠀⢀⣀⣀⠀⠀⠀⢀⣀⣀⣀⡀                     115C
      ⢀⠔⠁⠀⠀⠉⠢⡀⠀⢀⠔⠁⠀⠀⠈                      30C
      last 45s      temp tj   duty
────────────────────────────────────────────────────
[↑/↓] ±5%   [1-9] 10-90%   [0] 100%   [c]urve  [m]anual  [v]endor  [g]raph  [q]uit
```

| 按键 | 作用 |
|---|---|
| `↑` `↓`（或 `k` `j`） | 占空比 ±5%，相对当前转速 |
| `1`–`9` | 直接跳到 10%–90% |
| `0` | 满速 100% |
| `c` | 曲线模式：按配置的温度曲线自动调速 |
| `m` | 手动模式：保持固定占空比 |
| `v` | 原厂模式：把风扇交回 `nvfancontrol` |
| `g` | 开关曲线图 |
| `q` 或 `Esc` | 退出，同时释放风扇 |

怎么读这个界面：

- 转速条上的 `┃` 是原厂 5371 RPM 上限。它右边全是原厂从不使用的余量。
- `*` 标记当前最热的一路。那一路同时是曲线图画的、控制曲线读的、联锁盯的。
- `●` 表示该子系统**确实在降频**，`○` 表示没有。`freq` 那行只在真的被限频时出现。
- 曲线图画的是最热那一路的温度（青色）和占空比（暗色），各按自己量程缩放。它的
  用途是看**响应形状**：风扇反应快不快、有没有过冲。图上的缺口是真实的数据缺失，
  不是显示故障。

以 root 运行且没有现存 daemon 时，界面会为本次会话启动自己的控制循环，退出时交回
风扇，不需要额外操作。如果已有 daemon 在跑则直接连过去，绝不取代它。没有 root
也不在组里时界面仍可运行，只读。

### 命令行调速

```bash
sudo thorfan set 90%      # 百分比
sudo thorfan set 255      # 裸数字按 pwm 解释，0-255
sudo thorfan mode curve   # 切到温度曲线
sudo thorfan mode vendor  # 交回风扇
```

有 daemon 在跑时，这些命令通过控制 socket 立即生效。没有时，`set` 和 `mode` 会在
**前台**起一个控制循环并带着设定值启动，中断时释放风扇。加 `--save` 则只把设定
写进配置文件给 systemd 服务用，不立即起循环。

`set` **不打印结果转速**，因为转速表滞后好几秒。稍等片刻跑 `thorfan status` 看
稳定值。

命令行永远不直接写 `pwm1`。100 °C 联锁活在控制循环里，一个写完就退出的进程留下
的风扇没有任何联锁。这就是为什么改速总是意味着要有一个运行中的循环。

### 其他命令

```bash
thorfan profile           # 原厂曲线，并标出 RPM 上限
sudo thorfan daemon       # 只跑控制循环，systemd 单元用的就是这个
sudo thorfan restore      # 把风扇交回 nvfancontrol
```

### 配置曲线

曲线模式读 `/etc/thorfan/config.json`。目前还没有编辑器，需要手写：

```bash
sudo mkdir -p /etc/thorfan
sudo tee /etc/thorfan/config.json > /dev/null <<'EOF'
{
  "version": 1,
  "mode": "curve",
  "manual_pwm": 128,
  "poll_interval_s": 2.0,
  "curve": [
    {"temp_c": 40,  "pwm": 77},
    {"temp_c": 60,  "pwm": 90},
    {"temp_c": 75,  "pwm": 120},
    {"temp_c": 85,  "pwm": 165},
    {"temp_c": 95,  "pwm": 210},
    {"temp_c": 100, "pwm": 255}
  ]
}
EOF
```

`temp_c` 是**绝对温度**，这一点和原厂 profile 不同——原厂第一列是距临界点的余量，
方向是反的。40 °C 那档的 pwm 77 与原厂最低档一致，所以空闲噪音不变；差别在高温
段，原厂到顶只给 pwm 97，这条一路给到 255。

配置文件格式错误时会退回原厂模式和默认曲线，而不是拒绝启动。

## 工作原理

三种模式：

- **vendor（原厂）** —— `nvfancontrol` 掌管风扇，ThorFAN 只观察从不写入，所以
  两者不会争抢 `pwm1`。
- **curve（曲线）** —— ThorFAN 停止 `nvfancontrol`，按分段线性的温度/占空比曲线
  驱动风扇。
- **manual（手动）** —— 固定占空比。

### 安全机制

在一块空闲就接近 108 °C 的板子上，给界面「停风扇」的能力必须配联锁：

- **100 °C 以上**任何用户设定都被推翻，风扇强制满速。这个覆盖会锁存，直到温度
  降到 95 °C 以下才解除。
- 停止 daemon 一定会重启 `nvfancontrol`，包括崩溃和 `SIGTERM` 的情况。如果原厂
  守护进程拉不起来，风扇被强制满速而不是保持在任意值。
- 如果**所有**温度传感器都读不出来，风扇满速——没有温度就无法安全地猜测。
- 配置文件损坏或手改坏时退回原厂模式，而不是拒绝启动。

一个已知缺口：`SIGKILL` 会绕过上述所有机制，风扇停在最后设定值。只有 systemd
单元的 `ExecStopPost` 能覆盖这种情况，这也是长期运行建议用服务而非前台 daemon
的原因。

## 预期管理

满速在实测机器上只换来 5.8 °C。散热器和风道已接近饱和，所以风扇不是脱离降频区间
的杠杆——降低 `nvpmodel` 功耗档或缩小负载才是。这也是界面显示功耗轨的原因：负载
之下，是它们而不是风扇在决定温度。

## 平台笔记

以下细节都花了不少时间才搞清楚，记在这里以免他人重复实验：

**`pwm1_enable=0` 是关闭风扇，不是「手动模式」。** 输出级被禁用时写占空比完全
无效。要手动控速，必须**先写 `pwm1`，再写 `pwm1_enable=1`**。

**原厂 profile 的第一列不是温度。** 在 `TMARGIN ENABLED` 下它表示距临界温度的
余量，所以**数值越小越热**。`0 0 255 5371` 这一行是在 SoC 顶到上限时生效，不是
冷机时。

**`close_loop` 模式下 PWM 列只是参考。** `nvfancontrol` 追的是 RPM 列，这解释了
为什么表里写 255 而实际 `pwm1` 只有 97。

**Tegra 温度传感器会间歇返回 `EAGAIN`。** 直接读
`/sys/class/thermal/thermal_zone*/temp` 在负载下偶发失败，必须重试。

**`gpu-thermal` 在 GPU 被断电门控时完全读不出来。** 这和上面那条不是一回事：
GPU 空闲时该 zone **每一次**读取都返回 `EAGAIN`（实测 300 次全失败），有负载后
恢复正常。重试无法解决，所以读不出来的 zone 直接跳过而不当作错误。

如果你要在此基础上开发，有一条值得知道：**对一个永久失败的读取做重试，退避就变成
了固定延迟。** 重试阶梯对每个 gated zone 要花 30 ms，在把可选传感器的读取路径改成
单次不重试之前，这占了界面单帧开销的 80%。

**hwmon 编号不稳定。** 开发机上 `pwm_tach` 在两次重启之间从 `hwmon4` 变成了
`hwmon2`。必须按 `name` 文件的内容查找设备。`cooling_device*` 的编号也不能靠排序
——字典序下 `cooling_device10` 排在 `cooling_device2` 前面。

**降频告警比温度更直接。** `*-throttle-alert` 这些 cooling device 在 SoC 真的被
降频时数值大于 0，`devfreq-gpu-gpc-0` 报告当前 GPU 频率上限。

**INA3221 只有三路是真实电源轨。** `in1`–`in3` 带 label 且有对应的 `curr*_input`
节点，其余是分流电压和求和值，算进总功耗会重复计数。

**`/etc/nvfancontrol.conf` 不属于任何 deb 包。** `nvidia-l4t-nvfancontrol` 包只
提供 `/etc/nvpower/nvfancontrol/` 下的模板，由 `nvpower` 按板卡型号复制到位。改这
个文件不会被包升级覆盖，但重新刷机会重置。

## 恢复现场

出问题时：

```bash
sudo systemctl stop thorfan        # 如果装了服务
sudo thorfan restore
thorfan status                     # 应看到 nvfancontrol=active
```

手动强制满速，注意写入顺序：

```bash
sudo systemctl stop nvfancontrol
sudo sh -c 'echo 255 > /sys/class/hwmon/hwmon1/pwm1; echo 1 > /sys/class/hwmon/hwmon1/pwm1_enable'
```

先用 `cat /sys/class/hwmon/hwmon*/name` 确认 hwmon 编号，因为它不稳定。

## 开发

```bash
python3 -m pytest tests/ -q     # 138 项测试，不需要硬件
```

测试会伪造 sysfs，所以在任何机器上都能跑。终端界面需要真终端；冒烟测试可以这样：

```bash
timeout 10 script -qc "TERM=xterm-256color python3 -m thorfan.cli tui" /dev/null \
  < <(sleep 5; printf 'q')
```

改代码前请先读 [CONTRIBUTING.md](CONTRIBUTING.md)，里面列了不能破坏的安全性质。
[DEVELOPMENT.zh-CN.md](DEVELOPMENT.zh-CN.md) 记录了每个设计决定的理由和更完整的
硬件行为笔记。

## 许可证

MIT，见 [LICENSE](LICENSE)。

## 免责声明

ThorFAN 会解除原厂风扇转速上限，并允许把占空比设为任意值（包括停止风扇），而这
块硬件运行温度本就接近热限。上文描述的安全联锁是尽力而为的软件检查，不能替代原厂
散热策略。硬件损坏由使用者自行承担。请自行评估风险。
