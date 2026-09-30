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

默认流程不启用计量。将 `kenxu_meter_enabled` 设为 true 时，普通 Xray 模板更新后会重新同步受管客户端。`software.yml` 会阻止直接覆盖定制转发拓扑；SG2/UK 等定制主机应使用 `kenxu_meter.yml` 保留其原始配置。

- 新建/编辑加密变量
```bash
ansible-vault edit group_vars/all/vault.yml
```

- 查看加密内容
```bash
ansible-vault view group_vars/all/vault.yml
```
