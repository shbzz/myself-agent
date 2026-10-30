"""Versioned model/provider/context configuration with server-only credential files."""

import ipaddress
import json
import os
import re
import uuid
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ID = r"^[a-zA-Z0-9_-]{1,80}$"


class ConfigurationError(ValueError):
    pass


class ProviderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, pattern=ID)
    name: str = Field(min_length=1, max_length=80)
    protocol: str = Field(pattern=r"^(deepseek|openai_compatible)$")
    base_url: str = Field(min_length=8, max_length=1000)
    api_key: SecretStr | None = None
    remove_key: bool = False

    @model_validator(mode="after")
    def endpoint(self):
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in ("http", "https")
            or any(ord(c) < 32 for c in self.base_url)
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Base URL 须为不含账号、查询参数的 HTTP(S) 地址")
        return self


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, pattern=ID)
    name: str = Field(min_length=1, max_length=80)
    provider_id: str = Field(pattern=ID)
    model_id: str = Field(min_length=1, max_length=200)
    context_window_tokens: int = Field(ge=8192, le=2097152)
    max_output_tokens: int = Field(ge=256, le=131072)
    output_token_parameter: str = Field(
        default="max_tokens", pattern=r"^(max_tokens|max_completion_tokens)$"
    )
    reasoning_off_value: str | None = Field(default=None, max_length=24)
    images: bool = False
    tools: bool = True
    streaming: bool = True
    reasoning_parameter: str = Field(default="none", pattern=r"^(none|deepseek|reasoning_effort)$")
    reasoning_levels: list[str] = Field(
        default_factory=lambda: ["off"], min_length=1, max_length=12
    )

    @model_validator(mode="after")
    def capabilities(self):
        if "off" not in self.reasoning_levels or any(
            not re.fullmatch(r"[a-zA-Z0-9_-]{1,24}", level) for level in self.reasoning_levels
        ):
            raise ValueError("思考等级须含 off，且只能使用短英文标识")
        if self.reasoning_parameter == "none" and self.reasoning_levels != ["off"]:
            raise ValueError("关闭思考参数映射时，等级只能是 off")
        if self.max_output_tokens >= self.context_window_tokens:
            raise ValueError("输出上限必须小于上下文容量")
        return self


def web_hostname(value):
    host = value.strip().lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("网页白名单使用完整域名，不使用 IP 地址")
    if (
        len(host) > 253
        or "." not in host
        or host.endswith((".local", ".localhost", ".internal", ".lan"))
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in host.split(".")
        )
    ):
        raise ValueError("请输入完整公网域名，不含协议、路径或通配符")
    return host


class MCPServerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, pattern=ID)
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=1024)
    allowed_tools: list[str] = Field(default_factory=list, max_length=40)
    api_key: SecretStr | None = None
    remove_key: bool = False

    @model_validator(mode="after")
    def endpoint(self):
        parsed = urlsplit(self.url)
        try:
            port = parsed.port
            address = ipaddress.ip_address(parsed.hostname or "")
            private_ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
            local = address.is_loopback or any(
                address in ipaddress.ip_network(network) for network in private_ranges
            )
        except ValueError:
            local = parsed.hostname == "localhost"
            port = None
        if (
            parsed.scheme not in ("http", "https")
            or any(ord(c) < 32 for c in self.url)
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "http" and not local)
        ):
            raise ValueError("MCP 使用 HTTPS 地址；HTTP 仅用于明确的本机或私网 IP")
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("MCP 端口无效") from None
        if port == 0 or any(
            not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,128}", name) for name in self.allowed_tools
        ):
            raise ValueError("MCP 工具名或端口无效；工具白名单不支持通配符")
        self.allowed_tools = list(dict.fromkeys(self.allowed_tools))
        return self


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_profile_id: str | None = Field(default=None, pattern=ID)
    effective_context_tokens: int | None = Field(default=None, ge=8192, le=2097152)
    output_reserve_tokens: int | None = Field(default=None, ge=256, le=131072)
    compression_trigger_ratio: float | None = Field(default=None, ge=0.5, le=0.95)
    compression_target_ratio: float | None = Field(default=None, ge=0.15, le=0.8)
    keep_recent_messages: int | None = Field(default=None, ge=2, le=40)
    retained_images: int | None = Field(default=None, ge=0, le=8)
    max_model_calls: int | None = Field(default=None, ge=1, le=100)
    timeout_seconds: int | None = Field(default=None, ge=30, le=1800)
    tool_timeout_seconds: int | None = Field(default=None, ge=1, le=60)
    web_enabled: bool | None = None
    browser_enabled: bool | None = None
    allowed_web_hosts: list[str] | None = Field(default=None, max_length=32)
    max_web_requests: int | None = Field(default=None, ge=1, le=50)
    max_mcp_calls: int | None = Field(default=None, ge=1, le=50)
    mcp_server_ids: list[str] | None = Field(default=None, max_length=20)

    @field_validator("allowed_web_hosts")
    @classmethod
    def domains(cls, values):
        return list(dict.fromkeys(web_hostname(v) for v in values)) if values is not None else None

    @field_validator("mcp_server_ids")
    @classmethod
    def servers(cls, values):
        if values is not None and any(not re.fullmatch(ID, value) for value in values):
            raise ValueError("MCP 服务 ID 无效")
        return list(dict.fromkeys(values)) if values is not None else None


DEFAULT_POLICY = {
    "model_profile_id": "deepseek-flash",
    "effective_context_tokens": 131072,
    "output_reserve_tokens": 6000,
    "compression_trigger_ratio": 0.8,
    "compression_target_ratio": 0.5,
    "keep_recent_messages": 8,
    "retained_images": 2,
    "max_model_calls": 24,
    "timeout_seconds": 600,
    "tool_timeout_seconds": 30,
    "web_enabled": False,
    "browser_enabled": False,
    "allowed_web_hosts": [],
    "max_web_requests": 20,
    "mcp_server_ids": [],
    "max_mcp_calls": 20,
}


class ConfigurationStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS configuration (kind TEXT NOT NULL, "
                "id TEXT NOT NULL, version INTEGER NOT NULL, value TEXT NOT NULL, "
                "PRIMARY KEY(kind,id,version))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS context_records (id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, session_id TEXT NOT NULL, run_id TEXT NOT NULL, "
                "value TEXT NOT NULL)"
            )
            seeds = {
                ("provider", "deepseek"): {
                    "id": "deepseek",
                    "name": "DeepSeek 官方",
                    "protocol": "deepseek",
                    "base_url": "https://api.deepseek.com",
                    "credential_ref": "builtin:deepseek",
                },
                ("model", "deepseek-flash"): ModelInput(
                    id="deepseek-flash",
                    name="DeepSeek Flash",
                    provider_id="deepseek",
                    model_id="deepseek-flash",
                    context_window_tokens=1048576,
                    max_output_tokens=8192,
                    images=True,
                    tools=True,
                    streaming=True,
                    reasoning_parameter="deepseek",
                    reasoning_levels=["off", "low", "high", "max"],
                ).model_dump(),
                ("policy", "global"): DEFAULT_POLICY,
            }
            for (kind, identifier), value in seeds.items():
                db.execute(
                    "INSERT OR IGNORE INTO configuration VALUES (?,?,1,?)",
                    (kind, identifier, json.dumps(value)),
                )

    def get(self, kind, identifier):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM configuration WHERE kind=? AND id=? ORDER BY version DESC LIMIT 1",
                (kind, identifier),
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return json.loads(row["value"]) | {"version": row["version"]}

    def list(self, kind):
        with self.store.connect() as db:
            identifiers = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT id FROM configuration WHERE kind=? ORDER BY id", (kind,)
                )
            ]
        return [self.public(self.get(kind, identifier)) for identifier in identifiers]

    @staticmethod
    def public(value):
        value = dict(value)
        if "credential_ref" in value:
            value["has_key"] = bool(value.pop("credential_ref"))
        return value

    def write(self, kind, identifier, value):
        value = dict(value)
        value.pop("version", None)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM configuration WHERE kind=? AND id=?",
                (kind, identifier),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO configuration VALUES (?,?,?,?)",
                (kind, identifier, version, json.dumps(value)),
            )
        return self.public(value | {"version": version})

    def credential_reference(self, kind, identifier, body):
        try:
            credential_ref = self.get(kind, identifier).get("credential_ref")
        except KeyError:
            credential_ref = None
        if body.remove_key:
            credential_ref = None
        elif body.api_key and body.api_key.get_secret_value().strip():
            secret = body.api_key.get_secret_value().strip()
            if len(secret) > 8192 or "\n" in secret or "\r" in secret:
                raise ConfigurationError("密钥格式不正确")
            directory = self.store.root / "credentials"
            directory.mkdir(mode=0o700, exist_ok=True)
            credential_ref = uuid.uuid4().hex
            fd = os.open(directory / credential_ref, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(secret)
        return credential_ref

    def save_provider(self, body):
        identifier = body.id or uuid.uuid4().hex
        value = body.model_dump(exclude={"api_key", "remove_key"}) | {
            "id": identifier,
            "credential_ref": self.credential_reference("provider", identifier, body),
        }
        return self.write("provider", identifier, value)

    def save_mcp_server(self, body):
        identifier = body.id or uuid.uuid4().hex
        value = body.model_dump(exclude={"api_key", "remove_key"}) | {
            "id": identifier,
            "credential_ref": self.credential_reference("mcp", identifier, body),
        }
        return self.write("mcp", identifier, value)

    @classmethod
    def public_snapshot(cls, value):
        return dict(
            value,
            provider=cls.public(value["provider"]),
            mcp_servers=[cls.public(server) for server in value.get("mcp_servers", [])],
        )

    def save_model(self, body):
        self.get("provider", body.provider_id)
        identifier = body.id or uuid.uuid4().hex
        return self.write("model", identifier, body.model_dump() | {"id": identifier})

    def resolve(self, workspace_id=None, session_id=None, override=None):
        sources = ["global"]
        if session_id:
            workspace_id = self.store.session(session_id)["workspace_id"]
        if workspace_id:
            self.store.workspace(workspace_id)
            sources.append("workspace:" + workspace_id)
        if session_id:
            sources.append("session:" + session_id)
        policy = dict(DEFAULT_POLICY)
        revisions = {}
        for source in sources:
            try:
                row = self.get("policy", source)
            except KeyError:
                continue
            revisions[source] = row.pop("version")
            policy.update(row)
        if override:
            policy.update(override)
        profile = self.get("model", policy["model_profile_id"])
        provider = self.get("provider", profile["provider_id"])
        if policy["effective_context_tokens"] > profile["context_window_tokens"]:
            raise ConfigurationError("会话上下文预算不能超过模型容量，请同时调整预算")
        if policy["output_reserve_tokens"] > profile["max_output_tokens"]:
            raise ConfigurationError("输出预留不能超过模型输出上限")
        if policy["output_reserve_tokens"] + 2048 >= policy["effective_context_tokens"]:
            raise ConfigurationError("上下文须为输入和工具保留至少 2048 tokens")
        if policy["compression_target_ratio"] >= policy["compression_trigger_ratio"]:
            raise ConfigurationError("压缩目标比例须小于触发比例")
        if policy["web_enabled"] and not policy["allowed_web_hosts"]:
            raise ConfigurationError("启用网页工具前请设置允许访问的域名")
        if policy["browser_enabled"] and not policy["web_enabled"]:
            raise ConfigurationError("只读浏览器需要同时启用网页访问")
        servers = [self.get("mcp", identifier) for identifier in policy["mcp_server_ids"]]
        # Empty allowlists deny every tool, but must not lock users out of settings.
        return {
            "policy": policy,
            "profile": profile,
            "provider": provider,
            "mcp_servers": servers,
            "revisions": revisions,
        }

    def save_policy(self, scope, body):
        override = body.model_dump(exclude_none=True)
        if scope == "global":
            current = self.get("policy", "global")
            current.pop("version", None)
            current.update(override)
            self.resolve(override=current)
            return self.write("policy", scope, current)
        kind, _, identifier = scope.partition(":")
        if kind not in ("workspace", "session"):
            raise ConfigurationError("设置范围无效")
        self.resolve(**{kind + "_id": identifier}, override=override)
        try:
            previous = self.get("policy", scope)
        except KeyError:
            previous = {}
        previous.update(override)
        return self.write("policy", scope, previous)

    def records(self, session_id):
        self.store.session(session_id)
        with self.store.connect() as db:
            return [
                dict(r) | {"value": json.loads(r["value"])}
                for r in db.execute(
                    "SELECT * FROM context_records WHERE session_id=? ORDER BY id", (session_id,)
                )
            ]

    def record(self, session_id, run_id, value):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO context_records(session_id,run_id,value) VALUES (?,?,?)",
                (session_id, run_id, json.dumps(value, ensure_ascii=False)),
            )
