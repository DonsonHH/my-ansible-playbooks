## 如何使用

安装 ansible：https://docs.ansible.com/projects/ansible/latest/installation_guide/installation_distros.html  
并安装 ansible-runner 通过 Python 与 ansible 交互：`pip install ansible-runner`

将实例的信息填入 `inventory.ini` 中，示例：  
```ini
sin0 ansible_host=1.1.1.1 ansible_port=22 ansible_user="root" ansible_ssh_private_key_file="~/.ssh/sin0"
```
使用以下指令运行 ansible playbook:  
```bash
ansible-playbook common.yml -l $INVENTORY -t $TAG
```
或使用/自行编写 Python 脚本组合不同 playbook 中的不同 task  
> 可使用 Python 脚本中的 `MACHINE_NAME_LIST` 变量限制执行 playbook 的实例

## 目录结构
```
ansible/
├── ansible.cfg                 # ansible 全局配置
├── inventory.ini               # 实例相关信息，自行创建
├── group_vars/
│   └── all/
│       └── vault.yml          # 已加密的敏感变量文件，自行创建
└── vault_passwd                # Vault 密码文件，自行创建
```

## 常用操作

### 按用户流量计量（可选）

新增 [Kenxu Xray 计量部署说明](docs/kenxu-meter.md)。保留原客户端，并以独立 UUID 记录用户上传/下载。支持每个 Xray 核心一个采集器、多条逻辑线路，普通入口与 UK/SG2 可分别计量；第三方可使用独立的回环 WebSocket 中转入口。

来源 IP 与连接目标元数据保留 30 天。当前门户使用原生用户计数维护总账，使用独立的逐连接实际字节展示管理端网站去向，两者不叠加计费；1.7.0 起已删除旧版按分钟关联网站的算法与明细接口。用户个人中心只在最近使用 IP 后显示国家、省市归属估算。详见同一说明中的口径、隐私、缓存与部署顺序。

逐连接实际字节改造见 [设计](docs/kenxu-flow-design.md) 和 [构建、验收与分阶段上线](docs/kenxu-flow-rollout.md)。截至 2026-10-15，现有五台物理核心／六条逻辑线路已按 US2 隔离校准、单账号试点和逐台推广完成验收；JP／China 隐藏直连备用不计量。新主机仍默认关闭 flow，必须通过同样门槛，不因现有部署已通过就自动启用。不能把旧的关联区间改名为网站实际用量，也不重写历史账本。

默认流程不启用计量。在未固定 flow 核心的普通主机上，将 `kenxu_meter_enabled` 设为 true 时，普通 Xray 模板更新后会重新同步受管客户端。`software.yml` 会阻止直接覆盖固定 flow 核心和定制转发拓扑；现有主机通过 `kenxu_meter.yml` 维护采集器，核心升级另走经过验收的 `kenxu_flow_shadow.yml`，保留原始配置与动态队列。

2026-10-25 新主机与仓库复核见 [本轮记录](docs/review-20261025-new-host.md)。新加入的普通线路与限额住宅 IPv6 线路共用一个物理采集器，使用独立的用户身份和出站；住宅线路只对指定账号开放。JP 已取消，其历史记录保留。新主机的性能 CPU 门槛未通过后取得了针对该主机的明确例外批准，不能推广为其他机器默认放宽门槛。

首次开启访问日志时，采集器在 root 校验 Xray 配置之前先创建由 Xray 账号所有的私有日志，避免校验成功后服务因日志权限无法启动。现有日志内容不截断；Linux 文件权限回归测试覆盖此过程。

采集器的严格 systemd 沙箱还须允许写入 `/var/log/kenxu`，因为同步时的配置校验会打开访问日志。缺少该例外时，旧配置可以继续上报流量，但新增用户无法同步，门户会显示等待同步。模板和 flow 部署均已补齐权限；现有节点的最小修复、运行用户核验与回归结果见 [2026-10-28 修复记录](docs/review-20261028-meter-sync.md)。

- 新建/编辑加密变量
```bash
ansible-vault edit group_vars/all/vault.yml
```

- 查看加密内容
```bash
ansible-vault view group_vars/all/vault.yml
```
