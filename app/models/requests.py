"""请求模型"""

from pydantic import BaseModel


class LoginRequest(BaseModel):
    user: str
    password: str


class ServerRequest(BaseModel):
    name: str
    host: str
    port: int = 22
    user: str = "root"
    auth_type: str = "password"    # password | key
    password: str = ""
    ssh_key: str = ""              # SSH 私钥内容（PEM 格式）
    type: str = "ssh"
    tags: str = ""                 # 逗号分隔: prod,web


class TagRequest(BaseModel):
    name: str


class BotRequest(BaseModel):
    name: str
    type: str = "custom"           # dingtalk | wecom | custom
    webhook_url: str


class DeployRequest(BaseModel):
    project: str
    tag: str
    deploy_type: str = "ssh"       # ssh | compose | k8s
    server_ids: str = ""            # 逗号分隔的 server id，空=全部
    target_path: str = ""           # compose路径 / K8s YAML / Ansible playbook
    deploy_mode: str = ""           # docker | commands | ansible (SSH) / remote | commands (Compose)
    commands: str = ""              # 自定义命令，支持 {image} {tag} {project} 占位符
    yaml_content: str = ""          # 在线编写的 YAML，部署前写到服务器
    k8s_ns: str = ""                # K8s namespace
    k8s_deploy: str = ""            # K8s deployment 名
    k8s_container: str = ""         # K8s container 名
    bot_id: int = 0
