from workers.autonomous_execute import K8S_SDK_MUTATING_TOOL_NAMES, K8S_SDK_READONLY_TOOL_NAMES
from workers.tool_registry import get_tool_registry
from workers.tools import TOOL_REGISTRY


def test_k8s_new_tools_registered_in_both_registries():
    required = {
        "k8s_get_deployment_state",
        "k8s_list_workload_pods",
        "k8s_get_pod_secret_refs",
        "k8s_get_secret_keys",
        "k8s_verify_rollout",
        "k8s_patch_secret",
    }
    typed = get_tool_registry().tool_names()
    for name in required:
        assert name in typed
        assert name in TOOL_REGISTRY


def test_k8s_new_tools_classified_in_execute_allowlists():
    assert "k8s_patch_secret" in K8S_SDK_MUTATING_TOOL_NAMES
    assert "k8s_get_deployment_state" in K8S_SDK_READONLY_TOOL_NAMES
    assert "k8s_list_workload_pods" in K8S_SDK_READONLY_TOOL_NAMES
    assert "k8s_get_pod_secret_refs" in K8S_SDK_READONLY_TOOL_NAMES
    assert "k8s_get_secret_keys" in K8S_SDK_READONLY_TOOL_NAMES
    assert "k8s_verify_rollout" in K8S_SDK_READONLY_TOOL_NAMES
