"""Shipped auxiliary metric templates shared by both runtime generations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BuiltinTemplate:
    key: str
    name: str
    promql: str
    required_labels: tuple[str, ...]
    description: str


# These queries ship disabled. Operators must verify them against their own
# metric labels before enabling them; the primary curve remains alert-derived.
BUILTIN_TEMPLATES: tuple[BuiltinTemplate, ...] = (
    BuiltinTemplate(
        key="node_mem_available_ratio",
        name="节点内存可用率",
        promql=(
            'node_memory_MemAvailable_bytes{instance="{{instance}}"}'
            ' / node_memory_MemTotal_bytes{instance="{{instance}}"}'
        ),
        required_labels=("instance",),
        description="这个节点还剩多少内存可用，1 表示全空闲、0 表示耗尽。",
    ),
    BuiltinTemplate(
        key="node_cpu_busy_ratio",
        name="节点 CPU 使用率",
        promql=(
            "1 - avg without(cpu)("
            'rate(node_cpu_seconds_total{instance="{{instance}}",mode="idle"}[5m]))'
        ),
        required_labels=("instance",),
        description="这个节点的 CPU 有多忙，1 表示跑满。",
    ),
    BuiltinTemplate(
        key="node_fs_available_ratio",
        name="节点磁盘可用率",
        promql=(
            'node_filesystem_avail_bytes{instance="{{instance}}",fstype!~"tmpfs|overlay"}'
            ' / node_filesystem_size_bytes'
            '{instance="{{instance}}",fstype!~"tmpfs|overlay"}'
        ),
        required_labels=("instance",),
        description="这个节点每个挂载点还剩多少空间，接近 0 表示写满。",
    ),
    BuiltinTemplate(
        key="node_disk_io_saturation",
        name="节点磁盘 IO 繁忙度",
        promql='rate(node_disk_io_time_seconds_total{instance="{{instance}}"}[5m])',
        required_labels=("instance",),
        description="磁盘有多少时间在处理 IO，接近 1 表示 IO 已经排队。",
    ),
    BuiltinTemplate(
        key="pod_mem_vs_limit",
        name="Pod 内存用量占 limit 比例",
        promql=(
            "container_memory_working_set_bytes"
            '{namespace="{{namespace}}",pod="{{pod}}",container!=""}'
            " / on(namespace,pod,container) kube_pod_container_resource_limits"
            '{namespace="{{namespace}}",pod="{{pod}}",resource="memory"}'
        ),
        required_labels=("namespace", "pod"),
        description="这个 Pod 用掉了内存上限的多少，接近 1 就快被 OOMKill。",
    ),
    BuiltinTemplate(
        key="pod_cpu_throttle_ratio",
        name="Pod CPU 被限流比例",
        promql=(
            "rate(container_cpu_cfs_throttled_periods_total"
            '{namespace="{{namespace}}",pod="{{pod}}"}[5m])'
            " / rate(container_cpu_cfs_periods_total"
            '{namespace="{{namespace}}",pod="{{pod}}"}[5m])'
        ),
        required_labels=("namespace", "pod"),
        description="这个 Pod 有多少调度周期被 CPU limit 掐住，高说明它在被限速。",
    ),
    BuiltinTemplate(
        key="pod_container_restarts",
        name="容器重启次数（10 分钟增量）",
        promql=(
            "increase(kube_pod_container_status_restarts_total"
            '{namespace="{{namespace}}",pod="{{pod}}"}[10m])'
        ),
        required_labels=("namespace", "pod"),
        description=(
            "这个 Pod 的容器最近十分钟重启了几次。"
            "用 increase 而不是 rate：每秒重启数是个贴着 0 的小数，看不出刚才连着重启过。"
        ),
    ),
    BuiltinTemplate(
        key="pod_ready",
        name="Pod 是否就绪",
        promql=(
            'kube_pod_status_ready{namespace="{{namespace}}",pod="{{pod}}",condition="true"}'
        ),
        required_labels=("namespace", "pod"),
        description="这个 Pod 处于 Ready 的时间段，掉到 0 表示它退出了服务。",
    ),
)
